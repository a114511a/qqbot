"""端到端冒烟测试：真跑一遍 bot.py，验证插件能加载、指令能回复。

原理：在独立端口启动 bot.py，用 WebSocket 假装成 NapCat 建立 OneBot V11 反向连接，
发几条假的群聊/私聊消息，检查机器人是否按预期回复。不需要真的 QQ 和 NapCat。

用法（在项目根目录执行）：

    ./.venv/bin/python tests/smoke_test.py

改完插件后跑一次，能覆盖「插件加载失败、指令没注册、回复文案写错」这几类问题。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# 用独立端口，避免和真实 NapCat 的反向 WS（默认连 8080）互相干扰。
TEST_PORT = 8099
SELF_ID = "10000"
USER_ID = "10001"
GROUP_ID = "222222"
LOG_PATH = Path("/tmp/qqbot_smoke_test.log")

# (指令, 是否群聊, 回复里必须出现的关键词)
CHECKS: list[tuple[str, bool, list[str]]] = [
    ("/help", False, ["/底力分析", "/中二绑定", "/中二解绑", "/hello", "落雪", "群聊"]),
    ("/菜单", True, ["/底力分析", "群聊"]),
    ("/hello", True, ["喊我干啥"]),
    ("/底力分析", True, ["绑定"]),
    ("/中二绑定状态", False, ["没有绑定"]),
    ("/中二绑定", True, ["用法"]),
]


def wait_port(port: int, timeout: float = 40.0) -> bool:
    """等 uvicorn 把端口监听起来。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.3)
    return False


def build_event(text: str, message_id: int, group: bool) -> dict:
    """构造一条 OneBot V11 消息事件。"""
    event = {
        "post_type": "message",
        "message_id": message_id,
        "user_id": int(USER_ID),
        "message": text,
        "raw_message": text,
        "font": 0,
        "sender": {"user_id": int(USER_ID), "nickname": "tester", "sex": "unknown", "age": 0},
        "time": int(time.time()),
        "self_id": int(SELF_ID),
    }
    if group:
        event.update(
            {
                "message_type": "group",
                "sub_type": "normal",
                "group_id": int(GROUP_ID),
                "anonymous": None,
            }
        )
    else:
        event.update({"message_type": "private", "sub_type": "friend"})
    return event


async def ask(ws, text: str, message_id: int, group: bool) -> str:
    """发一条消息并取回机器人回复的纯文本。"""
    await ws.send(json.dumps(build_event(text, message_id, group)))
    while True:
        payload = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
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
            return segments
        return "".join(
            seg.get("data", {}).get("text", "") for seg in segments if isinstance(seg, dict)
        )


async def main() -> int:
    token = (dotenv_values(PROJECT_ROOT / ".env").get("ONEBOT_ACCESS_TOKEN") or "").strip()
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
            if token:
                headers["Authorization"] = f"Bearer {token}"

            async with websockets.connect(
                f"ws://127.0.0.1:{TEST_PORT}/onebot/v11/ws", additional_headers=headers
            ) as ws:
                await asyncio.sleep(1.0)  # 等适配器把 bot 注册进连接表
                for index, (command, group, expected) in enumerate(CHECKS, start=1):
                    reply = await ask(ws, command, index, group)
                    where = "群聊" if group else "私聊"
                    print(f"=== {where} {command} ===")
                    print(reply.strip()[:200])
                    for keyword in expected:
                        assert keyword in reply, f"{command} 回复缺少 {keyword!r}"
                    print(f"[OK] {where} {command}\n")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    log_text = LOG_PATH.read_text(encoding="utf-8")
    print("--- 插件加载日志 ---")
    for line in log_text.splitlines():
        if "Succeeded to load plugin" in line or "ERROR" in line:
            print(line)
    for plugin in ("chunithm_bot", "help", "hello"):
        assert f'Succeeded to load plugin "{plugin}"' in log_text, f"{plugin} 未加载成功"
    assert "ERROR" not in log_text, "启动日志出现 ERROR"

    print("\nPASS: 插件加载、群聊/私聊指令匹配与回复全部正常")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
