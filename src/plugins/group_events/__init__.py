"""群事件小工具：欢迎新成员 + 戳一戳回应。

自包含重写，替代旧项目里的 ``nonebot-plugin-eventmonitor 0.2.1``（GPLv3、
硬编码群号/昵称、config_check 有 KeyError bug、启动即 get_group_list）。
只保留这两个功能，去掉群荣誉/运气王/退群/管理员变动/群文件等无关逻辑。

配置（.env）：
    WELCOME_ENABLED=true          是否启用欢迎
    WELCOME_GROUPS=[]             只在指定群启用，留空 = 所有群
    POKE_ENABLED=true             是否启用戳一戳
    POKE_GROUPS=[]                同上
    POKE_CD=2                     每人冷却秒数，0 = 不限
"""

from __future__ import annotations

import random
import time
from typing import Optional

from nonebot import get_driver, get_plugin_config, logger, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupIncreaseNoticeEvent,
    Message,
    MessageSegment,
    PokeNotifyEvent,
)
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel


class Config(BaseModel):
    welcome_enabled: bool = True
    poke_enabled: bool = True
    poke_cd: int = 2
    # 默认所有群生效。只有在确实需要屏蔽某些群时，才往这里填群号。
    disabled_groups: list[str] = []


config = get_plugin_config(Config)
driver = get_driver()

__plugin_meta__ = PluginMetadata(
    name="群事件小工具",
    description="新成员进群欢迎 + 戳一戳随机回应",
    usage="进群自动欢迎；戳一戳 bot 会随机回一句",
    type="application",
    supported_adapters={"~onebot.v11"},
)

# --------------------------------------------------------------------------
# 欢迎新成员
# --------------------------------------------------------------------------


def _group_allowed(group_id: int | None) -> bool:
    """所有群默认生效；只有显式列进 disabled_groups 的群才跳过。"""
    if group_id is None:
        return True
    return str(group_id) not in config.disabled_groups


def _welcome_msg(user_id: int, group_id: int, bot_qq: int) -> Message:
    avatar = MessageSegment.image(
        f"https://q4.qlogo.cn/headimg_dl?dst_uin={user_id}&spec=640"
    )
    if user_id == bot_qq:
        return Message("本喵被邀进入贵群喵~\n让我看看这是哪里喵~")

    text = (
        "✨ 成员变动 ✨\n"
        + MessageSegment.at(user_id)
        + " 欢迎加入喵~\n"
        + f"加入时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        + "请在群内积极发言喵~，记得查看群公告哦"
    )
    return Message(text) + avatar


welcome = on_notice(
    Rule(lambda event: isinstance(event, GroupIncreaseNoticeEvent)),
    priority=50,
    block=True,
)


@welcome.handle()
async def handle_welcome(bot: Bot, event: GroupIncreaseNoticeEvent):
    if not config.welcome_enabled:
        return
    if not _group_allowed(event.group_id):
        return
    try:
        bot_qq = int(bot.self_id)
    except (TypeError, ValueError):
        bot_qq = 0
    await welcome.finish(_welcome_msg(event.user_id, event.group_id, bot_qq))


# --------------------------------------------------------------------------
# 戳一戳
# --------------------------------------------------------------------------

POKE_MESSAGES = [
    "别戳了，怕疼 QwQ",
    "呜呜，再戳脸都要肿了",
    "再戳我要叫主人了喵~",
    "别老戳了，您歇会吧~",
    "喂(#`O′) 戳我干嘛！",
    "正在关闭对您的所有服务...关闭成功（并没有）",
    "戳一下又不会上分，别戳啦",
    "再戳我就把你的 b50 藏起来喵",
    "今天已经被戳 N 次了，记小本本上",
    "乖，摸摸头，别戳了",
    "戳够了没，我要去打歌了",
    "你戳到我的名牌了，很贵的喵",
    "再戳一下试试？（并不会怎样）",
    "检测到戳一戳，已自动回戳 ✓",
    "别戳啦，Rating 不会涨的",
    "别戳了，江江怕疼",
    "在戳我要找人弄你了喵",
]

_poke_last: dict[str, float] = {}


def _is_poke(event) -> bool:
    return isinstance(event, PokeNotifyEvent) and event.is_tome()


poke = on_notice(Rule(_is_poke), priority=50, block=True)


@poke.handle()
async def handle_poke(event: PokeNotifyEvent):
    if not config.poke_enabled:
        return
    group_id: Optional[int] = getattr(event, "group_id", None)
    if not _group_allowed(group_id):
        return

    uid = str(event.get_user_id())
    now = time.monotonic()
    cd = max(0, config.poke_cd)
    if cd and now - _poke_last.get(uid, 0.0) < cd:
        return  # 冷却中，静默忽略，不刷屏
    _poke_last[uid] = now

    await poke.finish(Message(random.choice(POKE_MESSAGES)))


@driver.on_startup
async def _log_status():
    logger.info(
        f"[群事件] 欢迎={'开' if config.welcome_enabled else '关'} "
        f"戳一戳={'开' if config.poke_enabled else '关'} "
        f"(CD={config.poke_cd}s)"
    )
