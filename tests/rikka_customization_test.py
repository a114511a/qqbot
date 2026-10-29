"""Rikka 插件改造验收测试（离线，不联网）。

守住这次改造的四条需求，防止以后改动时回退：

1. **不需要 @机器人**：所有 matcher 不再注册 ``to_me()`` 规则，
   群聊里直接发指令就能触发。
2. **没有点前缀**：指令前缀只有 ``/``，不再支持 ``.mai b50`` 这种写法。
3. **共享落雪绑定**：用户在底力分析插件里绑定过之后，
   Rikka 能直接读到好友码，不需要重复绑定；落雪令牌也复用 ``LXNS_DEV_TOKEN``。
4. **help 是索引结构**：裸 ``/help`` 只给分类入口，
   ``/help mai`` / ``/help chu`` / ``/help bind`` 给具体指令。
5. **店铺查询已删除**：不再有 location 相关指令与 handler。

运行：
    ./.venv/bin/python tests/rikka_customization_test.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PROJECT_ROOT / "src" / "plugins" / "nonebot_plugin_rikka"
ALCONNA_PATH = PLUGIN_DIR / "alconna.py"
HELP_PATH = PROJECT_ROOT / "src" / "plugins" / "help.py"
SHARED_PATH = PLUGIN_DIR / "shared_binding.py"

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"[OK] {label}" + (f"  {detail}" if detail else ""))
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def main() -> int:
    alconna_src = ALCONNA_PATH.read_text(encoding="utf-8")
    help_src = HELP_PATH.read_text(encoding="utf-8")

    print("=== 1. 不需要 @机器人 ===")
    check("alconna.py 无 to_me 规则", "rule=to_me()" not in alconna_src)
    check("alconna.py 无 to_me 导入", "from nonebot.rule import to_me" not in alconna_src)
    # 所有 on_alconna 注册都不应带 rule=to_me
    registrations = re.findall(r"on_alconna\((.*?)\n\)", alconna_src, re.S)
    check("所有 on_alconna 注册数量正常", len(registrations) >= 20, f"共 {len(registrations)} 个")

    print("\n=== 2. 没有点前缀 ===")
    prefixes = re.search(r'COMMAND_PREFIXES\s*=\s*(\[[^\]]*\])', alconna_src)
    check("找到 COMMAND_PREFIXES 定义", prefixes is not None)
    if prefixes:
        value = prefixes.group(1)
        check("前缀只保留 /", value.strip() == '["/"]', value)
        check("前缀不含点号", '".' not in value, value)
    # 提示文案里不应再出现 ".指令名" 形式。
    # 注意要排除 Python 的 ".join(" / ".format(" 这类方法调用：
    # 真正的指令前缀后面跟的是空格、参数占位符 < > 或全角括号，而不是 "(" 紧跟字母。
    dot_hints = [
        m
        for m in re.findall(r'"\.[a-z][a-z0-9-]{1,}[\s<\[（]', alconna_src)
        if not m.startswith(('".join', '".format'))
    ]
    check("提示文案无点前缀指令", not dot_hints, f"残留 {dot_hints[:3]}")

    print("\n=== 3. 共享落雪绑定与令牌 ===")
    check("存在共享桥接模块", SHARED_PATH.exists())
    shared_src = SHARED_PATH.read_text(encoding="utf-8")
    check("提供 get_shared_friend_code", "def get_shared_friend_code" in shared_src)
    check("提供 save_shared_friend_code", "def save_shared_friend_code" in shared_src)
    check("提供 get_shared_lxns_token", "def get_shared_lxns_token" in shared_src)
    check("桥接到底力分析数据库", "chunithm.sqlite3" in shared_src)
    check("复用 LXNS_DEV_TOKEN", "LXNS_DEV_TOKEN" in shared_src)
    # 中二参数获取要优先查共享绑定
    check("_get_chu_params 查询共享绑定", "get_shared_friend_code(user_id)" in alconna_src)
    # provider 复用令牌
    chu_provider = (PLUGIN_DIR / "score/chunithm/providers/lxns.py").read_text(encoding="utf-8")
    check("中二 provider 复用共享令牌", "get_shared_lxns_token" in chu_provider)
    mai_provider = (PLUGIN_DIR / "score/maimai/providers/lxns.py").read_text(encoding="utf-8")
    check("舞萌 provider 复用共享令牌", "get_shared_lxns_token" in mai_provider)
    # 绑定时回写
    check("绑定后回写共享表", "save_shared_friend_code(user_id" in alconna_src)

    print("\n=== 4. help 索引结构 ===")
    for const in ("HELP_INDEX", "HELP_MAI", "HELP_CHU", "HELP_BIND"):
        check(f"help.py 定义 {const}", f"{const} =" in help_src)
    check("裸 /help 指向三类索引", "/help mai" in help_src and "/help chu" in help_src)
    check("支持子命令分发", "SUBCOMMANDS.get" in help_src)
    check("索引含 mai 键", '"mai":' in help_src)
    check("索引含 chu 键", '"chu":' in help_src)
    check("索引含 bind 键", '"bind":' in help_src)
    # Rikka 不再自己注册 /help，避免与 help.py 冲突
    check("Rikka 不再注册 help 指令", "alconna_help = on_alconna(" not in alconna_src)

    print("\n=== 5. 店铺查询已删除 ===")
    check("无 location-mai 指令", "location-mai" not in alconna_src)
    check("无 location-chu 指令", "location-chu" not in alconna_src)
    check("无 location handler", "handle_location" not in alconna_src)
    check("无 location 导入", "from .functions.location import" not in alconna_src)
    check("无店铺查询帮助文案", "店铺查询" not in alconna_src)

    print(f"\n共 {CHECKS} 项检查")
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过 -> {FAILURES}")
        return 1
    print("PASS: Rikka 插件改造全部符合预期")
    return 0


if __name__ == "__main__":
    sys.exit(main())
