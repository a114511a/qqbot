"""绑定与分析流程端到端测试（需要真实落雪令牌）。

和 smoke_test.py 的区别：smoke_test 不联网、只验证插件能加载和指令能回复；
本测试会真的调用落雪 API，验证「绑定 → 确认 → 出报告 → 解绑」整条链路。

用法（在项目根目录执行）：

    ./.venv/bin/python tests/binding_flow_test.py
    TEST_FRIEND_CODE=你的好友码 ./.venv/bin/python tests/binding_flow_test.py

测试用假的 QQ 号（10001）走流程，开跑前会清掉该 QQ 的残留绑定，结束时也会解绑，
不会影响真实用户的数据。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import websockets
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_PORT = 8099
SELF_ID, USER_ID = "10000", "10001"
# 好友码属于个人信息，不要写进仓库。跑这个测试前必须用环境变量提供：
#     TEST_FRIEND_CODE=你的好友码 ./.venv/bin/python tests/binding_flow_test.py
FRIEND_CODE = os.environ.get("TEST_FRIEND_CODE", "").strip()
LOG_PATH = Path("/tmp/qqbot_binding_flow_test.log")
DATABASE_PATH = PROJECT_ROOT / "data" / "chunithm.sqlite3"


def wait_port(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


async def ask(ws, text: str, message_id: int, count: int = 1) -> str:
    """发一条消息并取回回复。

    count > 1 用于 /底力分析 这类先提示、后出图的两段回复。
    """
    event = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": int(USER_ID),
        "message": text,
        "raw_message": text,
        "font": 0,
        "time": int(time.time()),
        "self_id": int(SELF_ID),
        "sender": {"user_id": int(USER_ID), "nickname": "tester", "sex": "unknown", "age": 0},
    }
    await ws.send(json.dumps(event))
    replies: list[str] = []
    while len(replies) < count:
        payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        # OneBot V11 适配器的 bot.send() 统一调用 send_msg。
        if payload.get("action") not in ("send_msg", "send_private_msg", "send_group_msg"):
            continue
        # 必须回执，否则机器人那边的 API 调用会一直等到超时。
        await ws.send(
            json.dumps(
                {
                    "status": "ok",
                    "retcode": 0,
                    "data": {"message_id": message_id},
                    "echo": payload.get("echo"),
                }
            )
        )
        segments = payload["params"].get("message", [])
        if isinstance(segments, str):
            replies.append(segments)
        else:
            replies.append(
                "".join(
                    seg.get("data", {}).get("text", "")
                    for seg in segments
                    if isinstance(seg, dict)
                )
            )
    return "\n".join(replies)


def binding_row(user_id: str) -> tuple | None:
    if not DATABASE_PATH.exists():
        return None
    with sqlite3.connect(DATABASE_PATH) as conn:
        return conn.execute(
            "SELECT friend_code, player_name FROM bindings WHERE user_id = ?", (user_id,)
        ).fetchone()


def clear_test_binding(user_id: str) -> None:
    """清掉测试 QQ 的绑定，保证测试可重复跑（上一轮中断时会留下残留）。"""
    if not DATABASE_PATH.exists():
        return
    with sqlite3.connect(DATABASE_PATH) as conn:
        conn.execute("DELETE FROM bindings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM pending_bindings WHERE user_id = ?", (user_id,))


async def main() -> int:
    env = dotenv_values(PROJECT_ROOT / ".env")
    if not (env.get("LXNS_DEV_TOKEN") or "").strip():
        print("跳过：.env 里没有 LXNS_DEV_TOKEN")
        return 1
    if not FRIEND_CODE:
        print(
            "跳过：没有提供测试好友码。\n"
            "本测试会真实调用落雪 API，需要一个有效的落雪好友码：\n"
            "    TEST_FRIEND_CODE=你的好友码 ./.venv/bin/python tests/binding_flow_test.py"
        )
        return 1
    # NoneBot 反向 WS 的握手 token 是 ONEBOT_ACCESS_TOKEN，别和落雪令牌搞混。
    ws_token = (env.get("ONEBOT_ACCESS_TOKEN") or "").strip()

    clear_test_binding(USER_ID)
    with LOG_PATH.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, "bot.py"],
            cwd=str(PROJECT_ROOT),
            env={**os.environ, "PORT": str(TEST_PORT)},
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        try:
            if not wait_port(TEST_PORT):
                print(f"bot.py 未在 {TEST_PORT} 端口监听，详见 {LOG_PATH}")
                return 1

            headers = {"X-Self-ID": SELF_ID, "X-Client-Role": "Universal"}
            if ws_token:
                headers["Authorization"] = f"Bearer {ws_token}"

            async with websockets.connect(
                f"ws://127.0.0.1:{TEST_PORT}/onebot/v11/ws", additional_headers=headers
            ) as ws:
                await asyncio.sleep(1.0)  # 等适配器把 bot 注册进连接表

                print("=== 1. 未绑定就查分析（应被拦下）===")
                reply = await ask(ws, "/底力分析", 1)
                print(" ", reply.strip()[:150])
                assert "没有绑定" in reply or "先发送" in reply, "未绑定时未被拦下"
                print("[OK]\n")

                print(f"=== 2. /中二绑定 {FRIEND_CODE} ===")
                reply = await ask(ws, f"/中二绑定 {FRIEND_CODE}", 2)
                print(" ", reply.strip()[:200])
                assert "查到落雪玩家" in reply, "未返回玩家资料"
                assert "确认" in reply, "未提示确认"
                print("[OK]\n")

                print("=== 3. /中二绑定状态（确认前应为未绑定）===")
                reply = await ask(ws, "/中二绑定状态", 3)
                print(" ", reply.strip()[:150])
                assert "没有绑定" in reply, "确认前不应已绑定"
                print("[OK]\n")

                print("=== 4. /中二绑定确认 ===")
                reply = await ask(ws, "/中二绑定确认", 4)
                print(" ", reply.strip()[:150])
                assert "已绑定落雪玩家" in reply, "确认未生效"
                print("[OK]\n")

                print("=== 5. 数据库落库检查 ===")
                row = binding_row(USER_ID)
                print("  bindings 行：", row)
                assert row is not None and row[0] == FRIEND_CODE, "绑定未写入数据库"
                print("[OK]\n")

                print("=== 6. /底力分析（先提示、后出图，共两条）===")
                reply = await ask(ws, "/底力分析", 6, count=2)
                print(" ", reply.strip()[:250])
                assert "正在读取" in reply, "未先发读取提示"
                assert "Rating" in reply, "分析未返回报告文案"
                # 出图成功后不该再冒出错误提示（finish 抛的 FinishedException 曾被误捕获）
                assert "报告生成或发送失败" not in reply, "成功出图后仍报了错"
                print("[OK]\n")

                print("=== 7. /中二解绑 ===")
                reply = await ask(ws, "/中二解绑", 7)
                print(" ", reply.strip()[:150])
                assert "已解绑" in reply, "解绑未生效"
                assert binding_row(USER_ID) is None, "解绑后数据库仍有残留"
                print("[OK] 已清理测试绑定\n")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    log_text = LOG_PATH.read_text(encoding="utf-8")
    print("--- 日志中的报错（应为空）---")
    errors = [line for line in log_text.splitlines() if "[ERROR]" in line]
    print("\n".join(errors) if errors else "（无）")
    assert not errors, "运行日志出现 ERROR"

    print("\nPASS: 绑定与分析流程端到端通过（真实落雪数据）")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
