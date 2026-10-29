"""Rikka 查分插件 · 与「中二节奏底力分析」插件的共享桥接层。

背景与目标
----------
本项目里有两个插件都要读落雪（LXNS）数据：

- ``chunithm_bot``：底力分析插件，自己维护 ``data/chunithm.sqlite3`` 的 ``bindings`` 表，
  用户在那边发 ``/中二绑定 好友码`` 完成绑定。
- ``nonebot_plugin_rikka``：本查分插件，原本用 ``nonebot_plugin_orm`` 自己的
  ``UserBindInfo`` 表，要求用户再发一次 ``/bind lxns <个人API密钥>``。

用户不希望同一个落雪账号绑两遍，所以这里做三件事（B 方案：共享一张绑定表）：

1. **好友码共享**：Rikka 取玩家身份时，先查 ``chunithm_bot`` 的 ``bindings`` 表，
   命中就直接用，不再要求重新绑定。
2. **落雪密钥共享**：Rikka 的开发者 API 密钥统一从项目 ``.env`` 的 ``LXNS_DEV_TOKEN``
   读取（由 ``chunithm_bot.lxns.get_token()`` 提供），不再单独配置
   ``lxns_developer_api_key``。
3. **写回**：如果用户走 Rikka 自己的 ``/bind lxns`` 绑定了落雪，也顺手写入
   ``chunithm_bot`` 的 ``bindings`` 表，让两个插件保持一致。

设计取舍
--------
``chunithm_bot`` 的表结构只有 ``user_id / friend_code / player_name / confirmed_at``，
没有落雪个人 API 密钥（底力分析用的是开发者密钥，不需要个人密钥）。
因此「共享」只覆盖好友码与开发者密钥；个人 API 密钥仍只存在 Rikka 自己的表里。
这满足「绑过就不用再绑」的需求，也不会把个人密钥写进另一个插件的库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from nonebot import logger

# 两个插件都在 src/plugins/ 下，向上两级即插件目录，再上一级是项目根。
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CHUNITHM_ANALYSIS_DB = PROJECT_ROOT / "data" / "chunithm.sqlite3"

# 与 chunithm_bot/analysis.py 的 DATABASE_PATH 指向同一个文件；
# 这里不直接 import 它，是为了避免 Rikka 加载时把底力分析插件也连带导入，
# 两者保持独立可加载（任何一方出问题都不影响另一方启动）。


def get_shared_friend_code(user_id: str) -> Optional[str]:
    """从底力分析插件的绑定库里读好友码；没有绑定或库不存在返回 None。

    只读操作，不会建库、不会写表。底力分析插件没装或没绑定过时静默返回 None，
    调用方继续走原有的绑定流程即可。
    """
    if not CHUNITHM_ANALYSIS_DB.exists():
        return None
    try:
        with sqlite3.connect(f"file:{CHUNITHM_ANALYSIS_DB}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT friend_code FROM bindings WHERE user_id = ?", (str(user_id),)
            ).fetchone()
    except sqlite3.Error as exc:
        logger.debug(f"[共享绑定] 读取底力分析绑定库失败: {exc}")
        return None
    if not row or not row[0]:
        return None
    return str(row[0])


def save_shared_friend_code(user_id: str, friend_code: str, player_name: str = "") -> bool:
    """把好友码写回底力分析插件的绑定库，让两边一致。

    仅在用户通过 Rikka 自己的 ``/bind lxns`` 绑定成功后调用。
    表不存在时（底力分析插件从未运行过）返回 False，不主动建表——
    建表是底力分析插件的职责，这里越权建表容易和它的 schema 迁移打架。
    """
    if not CHUNITHM_ANALYSIS_DB.exists():
        return False
    try:
        with sqlite3.connect(CHUNITHM_ANALYSIS_DB) as db:
            table = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='bindings'"
            ).fetchone()
            if not table:
                return False
            db.execute(
                "INSERT OR REPLACE INTO bindings (user_id, friend_code, player_name, confirmed_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (str(user_id), str(friend_code), player_name or ""),
            )
    except sqlite3.Error as exc:
        logger.debug(f"[共享绑定] 写回底力分析绑定库失败: {exc}")
        return False
    logger.debug(f"[共享绑定] 已把 {user_id} 的好友码写回底力分析绑定库")
    return True


def get_shared_lxns_token() -> str:
    """复用底力分析插件配置的落雪开发者 API 密钥（.env 的 LXNS_DEV_TOKEN）。

    这是「落雪的 API 使用底力分析的」这条需求的落点：
    Rikka 不再要求单独配置 ``LXNS_DEVELOPER_API_KEY``，两边读同一个令牌。
    读不到时返回空字符串，由调用方决定如何提示。
    """
    try:
        from ..chunithm_bot.lxns import get_env
    except Exception as exc:  # 底力分析插件缺失时不影响 Rikka 自身加载
        logger.debug(f"[共享绑定] 无法导入底力分析的 lxns 模块: {exc}")
        return ""
    return get_env("LXNS_DEV_TOKEN")
