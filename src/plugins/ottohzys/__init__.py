"""otto 活字印刷插件（自包含移植版）。

命令（与原插件一致，共 9 个别名）：
    otto活字印刷 / otto鬼叫 / ottohzys
    电棍活字印刷 / 电棍鬼叫 / 电棍hzys
    大电老师活字印刷 / 大电老师鬼叫 / 大电老师hzys

参数：
    -r, --reverse       倒放
    -Y, --no-ysdd       不使用原声大碟
    -N, --no-normalize  不使用音量归一化
    -s, --speed N       语速 >= 0.5，默认 1.0
    -p, --pitch N       音调 0 < N <= 2，默认 1.0
    -P, --pause N       停顿秒数，默认 0.25

示例：
    otto活字印刷 大家好啊
    otto鬼叫 -s 1.5 我阐述你的梦
"""

from __future__ import annotations

import argparse

from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.exception import FinishedException
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.adapters import Message

from .audio import RESOURCE_DIR, generate_voice_bytes

__plugin_meta__ = PluginMetadata(
    name="otto 活字印刷",
    description="把文本用 otto（电棍）语音切片拼成一句话，支持倒放/变速/变调",
    usage="otto活字印刷 [-r] [-Y] [-N] [-s 语速] [-p 音调] [-P 停顿] <文本>",
    type="application",
    homepage="https://github.com/HanaYabuki/otto-hzys",
    supported_adapters={"~onebot.v11"},
)

CMD_PREFIXES = ("otto", "电棍", "大电老师")
CMD_SUFFIXES = ("活字印刷", "鬼叫", "hzys")
COMMANDS = [f"{p}{s}" for p in CMD_PREFIXES for s in CMD_SUFFIXES]
PRIMARY, *ALIASES = COMMANDS

_parser = argparse.ArgumentParser(prog=PRIMARY, add_help=False)
_parser.add_argument("-r", "--reverse", action="store_true", help="倒放")
_parser.add_argument("-Y", "--no-ysdd", action="store_true", help="不使用原声大碟")
_parser.add_argument("-N", "--no-normalize", action="store_true", help="不使用音量归一化")
_parser.add_argument("-s", "--speed", type=float, default=1.0, help="语速 >= 0.5")
_parser.add_argument("-p", "--pitch", type=float, default=1.0, help="音调 0 < p <= 2")
_parser.add_argument("-P", "--pause", type=float, default=0.25, help="停顿秒数")
_parser.add_argument("sentence", nargs="*", help="要合成的文本")
HELP = _parser.format_help()
TIP_EXTREME = "过于极端的音调和速度参数可能导致输出结果与预期不符"

otto = on_command(PRIMARY, aliases=set(ALIASES), priority=10, block=True)


@get_driver().on_startup
async def _check_resources() -> None:
    import shutil

    tokens = RESOURCE_DIR / "tokens"
    n = len(list(tokens.glob("*.wav"))) if tokens.exists() else 0
    if n == 0:
        logger.warning(
            f"[otto] 未找到音节音频 {tokens}，活字印刷会全是静音。"
            "请运行 tools/extract_otto_assets.py 或手动放置 data/ottohzys/。"
        )
    else:
        logger.info(f"[otto] 已加载 {n} 个音节音频")
    if shutil.which("ffmpeg") is None:
        logger.warning("[otto] 未找到 ffmpeg，语音将回退为 WAV（部分协议端可能不支持）")


def _cleanup_tmp() -> None:
    """清掉一小时前生成的临时语音文件。"""
    import time

    tmp_dir = RESOURCE_DIR / "tmp"
    if not tmp_dir.exists():
        return
    cutoff = time.time() - 3600
    for p in tmp_dir.iterdir():
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


try:
    from nonebot import require

    require("nonebot_plugin_apscheduler")
    from nonebot_plugin_apscheduler import scheduler

    @scheduler.scheduled_job("interval", minutes=30, id="ottohzys_cleanup")
    async def _job_cleanup() -> None:
        _cleanup_tmp()

except Exception as e:  # apscheduler 不可用时退化为「每次生成时顺手清理」
    logger.debug(f"[otto] 未启用定时清理: {e}")


@otto.handle()
async def handle_otto(args: Message = CommandArg()):
    raw = args.extract_plain_text().strip()
    if not raw:
        await otto.finish(HELP)
        return

    try:
        ns = _parser.parse_args(raw.split())
    except SystemExit:
        await otto.finish(f"参数错误。\n{HELP}")
        return

    sentence = " ".join(ns.sentence).strip()
    if not sentence:
        await otto.finish(HELP)
        return
    if len(sentence) > 100:
        await otto.finish("文本太长了喵，最多 100 个字")
        return
    if ns.speed < 0.5 or not 0 < ns.pitch <= 2:
        await otto.finish(TIP_EXTREME)
        return

    try:
        voice = await generate_voice_bytes(
            sentence,
            reverse=ns.reverse,
            ysdd_mode=not ns.no_ysdd,
            normalize=not ns.no_normalize,
            speed=ns.speed,
            pitch=ns.pitch,
            pause=max(0.0, min(ns.pause, 2.0)),
        )
    except FinishedException:
        raise
    except Exception:
        logger.exception("[otto] 生成语音失败")
        await otto.finish("生成语音失败")
        return

    data = voice.getvalue()
    # 用 base64 内联发送语音：NapCat 跑在 Docker 里，宿主机的 file:// 路径
    # 在容器内不存在（retcode=1200 "Failed to open input"）。base64 不需要
    # 任何共享目录，NapCat 会自行解码。
    import base64

    is_mp3 = data[:3] == b"ID3" or data[:2] == b"\xff\xfb"
    b64 = base64.b64encode(data).decode()
    await otto.send(MessageSegment.record(file=f"base64://{b64}"))
    await otto.finish()
