"""中二节奏底力分析 · 指令层。

只保留 /底力分析。落雪绑定统一使用查分插件的
/bind lxns、/bind key 与 /unbind lxns，两个插件共用同一份绑定。
群聊和私聊都可以使用，绑定按发送者 QQ 区分，互不影响。

注意：群聊里成绩报告对同群成员可见，这一点在 /help 里已提示。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nonebot import on_command
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.exception import FinishedException

# 同包内的数据层，使用相对导入，插件改名/搬家都不受影响。
from .analysis import (
    AnalysisError,
    fetch_report,
    get_binding,
    napcat_report_path,
    render_report,
)

# 事件类型别名：群聊和私聊事件都会走到下面的处理函数。
MessageEvent = PrivateMessageEvent | GroupMessageEvent

analysis_command = on_command(
    "底力分析", aliases={"中二底力分析", "chunithm底力分析"}, priority=5, block=True
)


@analysis_command.handle()
async def handle_analysis(event: MessageEvent) -> None:
    """生成底力分析报告：取数 → 渲染图片 → 发送，最后清理临时文件。"""
    binding = get_binding(str(event.get_user_id()))
    if binding is None:
        await analysis_command.finish(
            "你还没有绑定落雪玩家。请先发送 /bind lxns <好友码>，"
            "或 /bind key <个人 API 密钥>（自动绑定好友码）。"
        )

    await analysis_command.send("正在读取你的落雪成绩并生成报告……")
    report_file: Path | None = None
    try:
        report = await fetch_report(binding["friend_code"], binding.get("api_key", ""))
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
