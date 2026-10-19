"""中二节奏底力分析 · 指令层。

注册并处理：/中二绑定、/中二绑定确认、/中二解绑、/中二绑定状态、/底力分析。
群聊和私聊都可以使用，绑定按发送者 QQ 区分，互不影响。

注意：群聊里好友码和成绩报告对同群成员可见，这一点在 /help 里已提示。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.exception import FinishedException
from nonebot.params import CommandArg

# 同包内的数据层，使用相对导入，插件改名/搬家都不受影响。
from .analysis import (
    AnalysisError,
    binding_confirmation_text,
    confirm_binding,
    create_pending_binding,
    fetch_player,
    fetch_report,
    get_binding,
    napcat_report_path,
    player_confirmation_text,
    remove_binding,
    render_report,
)

# 事件类型别名：群聊和私聊事件都会走到下面这些处理函数。
MessageEvent = PrivateMessageEvent | GroupMessageEvent

bind_command = on_command("中二绑定", aliases={"chunithm绑定"}, priority=5, block=True)
confirm_command = on_command("中二绑定确认", aliases={"中二确认绑定"}, priority=5, block=True)
unbind_command = on_command("中二解绑", aliases={"chunithm解绑"}, priority=5, block=True)
status_command = on_command("中二绑定状态", aliases={"中二状态"}, priority=5, block=True)
analysis_command = on_command(
    "底力分析", aliases={"中二底力分析", "chunithm底力分析"}, priority=5, block=True
)


@bind_command.handle()
async def handle_bind(
    event: MessageEvent,
    argument: Message = CommandArg(),
) -> None:
    """绑定落雪玩家：按好友码读取资料，展示确认信息后进入待确认状态。

    只认好友码，不做 QQ 关联核对——落雪不一定登记了用户的 QQ，
    硬核对会把没绑 QQ 的玩家直接挡在门外。
    """
    raw = argument.extract_plain_text().strip()
    # 好友码是纯数字，用前后否定环视避免把 "12345678abc" 之类的串当成好友码。
    match = re.search(r"(?<!\d)(\d{8,16})(?!\d)", raw)
    if not match:
        await bind_command.finish(
            "用法：/中二绑定 好友码\n请填写落雪中二节奏玩家好友码，不要发送落雪密码。"
        )
    friend_code = match.group(1)

    # 按好友码读取玩家资料，用于展示确认信息和后续报告。
    try:
        player = await fetch_player(friend_code)
    except AnalysisError as exc:
        await bind_command.finish(str(exc))
    except Exception:
        await bind_command.finish("读取落雪玩家资料失败，请稍后重试。")

    create_pending_binding(str(event.get_user_id()), friend_code, player)
    await bind_command.finish(player_confirmation_text(player))


@confirm_command.handle()
async def handle_confirm(event: MessageEvent) -> None:
    """确认绑定：把 10 分钟内的待确认记录转成正式绑定。"""
    binding = confirm_binding(str(event.get_user_id()))
    if binding is None:
        await confirm_command.finish("没有待确认绑定，或确认已超时。请重新执行 /中二绑定 好友码。")
    await confirm_command.finish(binding_confirmation_text(binding))


@unbind_command.handle()
async def handle_unbind(event: MessageEvent) -> None:
    """解绑：删除正式绑定和待确认记录。"""
    remove_binding(str(event.get_user_id()))
    await unbind_command.finish("已解绑落雪玩家资料，并清除待确认绑定。")


@status_command.handle()
async def handle_status(event: MessageEvent) -> None:
    """查询当前绑定状态。"""
    binding = get_binding(str(event.get_user_id()))
    if binding is None:
        await status_command.finish("当前没有绑定落雪玩家。发送 /中二绑定 好友码 开始绑定。")
    await status_command.finish(f"当前绑定玩家：{binding['player_name']}。发送 /中二解绑 可解除绑定。")


@analysis_command.handle()
async def handle_analysis(event: MessageEvent) -> None:
    """生成底力分析报告：取数 → 渲染图片 → 发送，最后清理临时文件。"""
    binding = get_binding(str(event.get_user_id()))
    if binding is None:
        await analysis_command.finish("你还没有绑定落雪玩家。先发送 /中二绑定 好友码，并按提示确认。")

    await analysis_command.send("正在读取你的落雪成绩并生成报告……")
    report_file: Path | None = None
    try:
        report = await fetch_report(binding["friend_code"])
        # 图片渲染是同步的 CPU 操作，丢到线程里避免阻塞事件循环。
        report_file = await asyncio.to_thread(render_report, report)
        image_uri = napcat_report_path(report_file)
        rating = report.get("rating")
        caption = (
            f"{report['player_name']} · Rating {float(rating):.2f}"
            if isinstance(rating, (int, float))
            else report["player_name"]
        )
        # 评价已经完整画在报告图里了，这里不再重复一遍——
        # 群聊里图片下面再贴一大段同样的文字，纯属刷屏。
        caption += f"\n数据截至：{report['generated_at']}"
        await analysis_command.finish(
            MessageSegment.image(image_uri) + MessageSegment.text("\n" + caption)
        )
    except FinishedException:
        # finish() 用抛异常来结束 matcher，而 FinishedException 是 Exception 的子类，
        # 不单独放行就会被下面的 except Exception 抓住，导致每次成功出图后又多发一条报错。
        raise
    except AnalysisError as exc:
        # AnalysisError 的文案已经面向用户，直接展示。
        await analysis_command.finish(str(exc))
    except Exception:
        await analysis_command.finish(
            "报告生成或发送失败。请确认 NapCat 与机器人已连接、data/reports 可读，并查看 NoneBot 日志。"
        )
    finally:
        # 发送完成后删掉临时图片，避免 data/reports 堆积。
        # finish() 抛异常时 finally 依然会执行，所以清理放在这里是安全的。
        if report_file is not None:
            try:
                report_file.unlink(missing_ok=True)
            except OSError:
                pass
