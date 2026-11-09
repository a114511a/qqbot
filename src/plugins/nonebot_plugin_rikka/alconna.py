import json
import random
import re
from asyncio import TimeoutError
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
from pathlib import Path
from time import perf_counter
from traceback import format_exc
from typing import Literal, Optional

from aiohttp.client_exceptions import ClientResponseError
from arclet.alconna import Alconna, AllParam, Args
from maimai_py import InvalidPlateError, LevelIndex, LXNSProvider
from maimai_py import SongType as MaimaiPySongType
from nonebot import get_driver, logger
from nonebot.adapters import Event
from nonebot.exception import FinishedException
from nonebot.matcher import current_bot, current_event
from nonebot.message import event_preprocessor
from nonebot.params import Depends
from nonebot_plugin_alconna import (
    AlconnaMatch,
    At,
    CommandMeta,
    Extension,
    Match,
    Subcommand,
    UniMessage,
    on_alconna,
)
from nonebot_plugin_alconna.extension import OutputType
from nonebot_plugin_alconna.uniseg import Image as UniImage
from nonebot_plugin_orm import async_scoped_session
from nonebot_plugin_session import (
    EventSession,
    SessionIdType,
    SessionLevel,
    extract_session,
)

from .config import config
from .shared_binding import (
    delete_shared_binding,
    get_shared_api_key,
    get_shared_friend_code,
    save_shared_api_key,
    save_shared_friend_code,
)
from .constants import CHU_VERSION_MAP, MAI_VERSION_MAP
from .database import (
    LocationSubscriptionORM,
    MaiPlayCountORM,
    MaiSongAliasORM,
    MaiSongORM,
    UserBindInfoORM,
)
from .database.crud import ChuSongORM
from .extra_proxy import (
    download_update_packages,
    get_maistatus,
    get_update_event,
    run_extend_score_workflow,
    run_extend_ticket_workflow,
    run_extent_force_logout,
    run_unlock_workflow,
)
from .functions.analysis import get_player_strength
from .functions.fortunate import generate_today_fortune
from .functions.lxns import (
    convert_to_lxns_maimai_format,
    get_updated_score,
    upload_to_lxns_maimai,
)
from .functions.maistatus import capture_maimai_status_png
from .functions.n50 import get_players_n50
from .functions.process import (
    ProcessDataError,
    get_level_process_data,
)
from .functions.recommend_songs import get_player_raise_score_songs
from .functions.song_tags import SONG_TAGS_DATA_AVAILABLE, get_songs_tags
from .models.chu_song import ChuSong
from .models.song import MaiSong
from .painters import (
    DrawScores,
    draw_player_rating_trend,
    draw_player_strength_analysis,
    image_to_bytes,
)
from .renderer import ChuPicRenderer, MaiPicRenderer
from .score.chunithm import (
    LXNSChuScoreProvider,
    PlayerChuScore,
    get_lxns_chu_provider,
)
from .score.chunithm.providers.lxns import LXNSChuParams
from .score.maimai import (
    DivingFishParams,
    DivingFishScoreProvider,
    LXNSScoreProvider,
    MaimaiPyScoreProvider,
    get_divingfish_provider,
    get_lxns_provider,
    get_maimaipy_provider,
)
from .score.maimai.providers.lxns import LXNSParams, LXNSRatingTrend
from .score.maimai.providers.maimai import MaimaiPyParams, maimai_client
from .updater.songs import (
    update_chu_song_database,
    update_local_chart_file,
    update_maimai_song_database,
    update_song_alias_list,
)
from .utils import get_chusong_by_id_or_alias, get_maisong_by_id_or_alias, is_float

renderer = MaiPicRenderer()
chu_renderer = ChuPicRenderer()


async def _send_message(
    target: str,
    message: str,
    images: Optional[list[bytes | BytesIO]] = None,
    *,
    finish: bool = False,
    at: Optional[bool] = None,
):
    if at is None:
        session = extract_session(current_bot.get(), current_event.get())
        at = session.level in (SessionLevel.LEVEL2, SessionLevel.LEVEL3)

    response = UniMessage()
    if at:
        response.append(At(flag="user", target=target))
    if message:
        response += message
    for image in images or []:
        response.append(UniImage(raw=image))

    if finish:
        await response.finish()
    else:
        await response.send()


def _build_maisong_info_message(song: MaiSong) -> tuple[str, list[bytes | BytesIO]]:
    """Reuseable builder for song info replies."""

    song_cover = Path(config.static_resource_path) / "mai" / "cover" / f"{song.id}.png"

    if not song_cover.exists():
        dx_song_id = song.id + 10000  # DX 版封面
        song_cover = Path(config.static_resource_path) / "mai" / "cover" / f"{dx_song_id}.png"
        if not song_cover.exists():
            logger.warning(f"未找到乐曲 {song.id} 的封面图片")
            song_cover = Path(config.static_resource_path) / "mai" / "cover" / "0.png"

    response_difficulties_content = []

    if song.difficulties.standard:
        std_diffs = "/".join([str(diff.level_value) for diff in song.difficulties.standard])
        response_difficulties_content.append(f"定数: {std_diffs}")

        if song.difficulties.standard[0].level_fit is not None:
            fit_diffs = "/".join(
                [
                    "{:.2f}".format(diff.level_fit) if diff.level_fit is not None else f"{diff.level}"
                    for diff in song.difficulties.standard
                ]
            )
            response_difficulties_content.append(f"拟合定数: {fit_diffs}")

        if SONG_TAGS_DATA_AVAILABLE:
            song_tags_content = []
            tags_expert = get_songs_tags(song.title, "std", "expert")
            tags_master = get_songs_tags(song.title, "std", "master")
            if tags_expert:
                song_tags_content.append(f"{', '.join(tags_expert)}(Expert)")
            if tags_master:
                song_tags_content.append(f"{', '.join(tags_master)}(Master)")
            if len(song.difficulties.standard) == 5:
                tags_remaster = get_songs_tags(song.title, "std", "remaster")
                song_tags_content.append(f"{', '.join(tags_remaster)}(Re:master)" if tags_remaster else "")
            if song_tags_content:
                response_difficulties_content.append("铺面标签: " + "; ".join(song_tags_content))

    if song.difficulties.dx:
        dx_diffs = "/".join([str(diff.level_value) for diff in song.difficulties.dx])
        response_difficulties_content.append(f"定数(DX): {dx_diffs}")

        if song.difficulties.dx[0].level_fit is not None:
            fit_diffs = "/".join(
                [
                    "{:.2f}".format(diff.level_fit) if diff.level_fit is not None else f"{diff.level}"
                    for diff in song.difficulties.dx
                ]
            )
            response_difficulties_content.append(f"拟合定数(DX): {fit_diffs}")

        if SONG_TAGS_DATA_AVAILABLE:
            song_tags_content = []
            tags_expert = get_songs_tags(song.title, "dx", "expert")
            tags_master = get_songs_tags(song.title, "dx", "master")
            if tags_expert:
                song_tags_content.append(f"{', '.join(tags_expert)}(Expert)")
            if tags_master:
                song_tags_content.append(f"{', '.join(tags_master)}(Master)")
            if len(song.difficulties.dx) == 5:
                tags_remaster = get_songs_tags(song.title, "dx", "remaster")
                song_tags_content.append(f"{', '.join(tags_remaster)}(Re:master)" if tags_remaster else "")
            if song_tags_content:
                response_difficulties_content.append("铺面标签(DX): " + "; ".join(song_tags_content))

    return (
        (
            _MAI_SONG_INFO_TEMPLATE.format(
                title=song.title,
                id=song.id,
                artist=song.artist,
                genre=song.genre,
                bpm=song.bpm,
                version=MAI_VERSION_MAP.get(song.version // 100, "未知版本"),
            )
        )
        + ("\n".join(response_difficulties_content)),
        [song_cover.read_bytes()],
    )


def _build_chusong_info_message(song: ChuSong) -> tuple[str, list[bytes | BytesIO]]:

    song_cover = Path(config.static_resource_path) / "chu" / "cover" / f"{song.id}.png"

    if not song_cover.exists():

        logger.warning(f"未找到中二节奏乐曲 {song.id} 的封面图片")

        song_cover = Path(config.static_resource_path) / "chu" / "cover" / "CHU_UI_Jacket_0000.png"

    diff_values = "/".join([str(d.level_value) for d in song.difficulties.difficulties])

    return (
        _CHU_SONG_INFO_TEMPLATE.format(
            title=song.title,
            id=song.id,
            artist=song.artist,
            genre=song.genre,
            bpm=song.bpm,
            version=CHU_VERSION_MAP.get(song.version, f"未知版本 ({song.version})"),
            diff_values=diff_values,
        ),
        [song_cover.read_bytes()],
    )


def catch_exception(reply_prefix: str = "发生了未知错误", reply_error: bool = True):

    def decorator(func):

        @wraps(func)
        async def wrapper(*args, **kwargs):

            try:

                return await func(*args, **kwargs)
            except FinishedException:
                raise
            except TimeoutError:
                logger.error(format_exc())
                reply_message = f"{reply_prefix}: 上游服务请求超时，或许等一下再试试？" if reply_error else reply_prefix
                await _send_message(current_event.get().get_user_id(), reply_message, finish=True)
            except Exception as exc:
                logger.error(format_exc())
                reply_message = f"{reply_prefix}: {str(exc)}" if reply_error else reply_prefix
                await _send_message(current_event.get().get_user_id(), reply_message, finish=True)

        return wrapper

    return decorator


def _parse_trend_time_window(raw_time_range: str) -> tuple[int | None, str | None]:
    """将时间入参解析为天数窗口。"""

    text = raw_time_range.strip().lower()
    if not text:
        return None, None

    normalized = text
    for token in ("以内", "之内", "内", "最近", "近"):
        normalized = normalized.replace(token, "")
    normalized = normalized.strip()

    match = re.fullmatch(
        r"(\d+)(天|日|周|星期|个月|個月|月|d|day|days|w|week|weeks|month|months)",
        normalized,
    )
    if not match:
        return None, None

    value = int(match.group(1))
    unit = match.group(2)
    if value <= 0:
        return None, None

    if unit in {"天", "日", "d", "day", "days"}:
        return value, f"近{value}天"
    if unit in {"周", "星期", "w", "week", "weeks"}:
        return value * 7, f"近{value}周"
    if unit in {"个月", "個月", "月", "month", "months"}:
        return value * 30, f"近{value}个月"

    return None, None


# 只保留 "/" 前缀：本项目统一用 /指令 风格，不再支持 "/mai b50" 这类点前缀写法。
COMMAND_PREFIXES = ["/"]

_MAI_SONG_INFO_TEMPLATE = """[舞萌DX] 乐曲信息
{title}({id})
艺术家: {artist}
分类: {genre}
BPM: {bpm}
版本: {version}
"""

_CHU_SONG_INFO_TEMPLATE = """[中二节奏] 乐曲信息
{title}({id})
艺术家: {artist}
分类: {genre}
BPM: {bpm}
版本: {version}
定数: {diff_values}
"""

_DIFFICULTY_VALUE_MAP = {"BASIC": 0, "ADVANCED": 1, "EXPERT": 2, "MASTER": 3, "REMASTER": 4, "RE:MASTER": 4}

_PARAM_UNMATCHED_PATTERN = re.compile(r"(参数\s*)([^\n]+?)(\s*匹配失败)")


class _MaskSensitiveExtension(Extension):
    """隐藏 Alconna 解析失败回复中回显的用户输入，避免泄露密钥等隐私。"""

    @property
    def id(self) -> str:
        return "rikka-mask-sensitive"

    @property
    def priority(self) -> int:
        return 1

    async def output_converter(self, output_type: OutputType, content: str) -> UniMessage:
        return UniMessage(_PARAM_UNMATCHED_PATTERN.sub(r"\1****\3", content))


alconna_bind = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "bind",
        Subcommand("help"),
        Subcommand(
            "lxns",
            Args["friend_code", str],
            help_text="/bind lxns <落雪好友码> 绑定落雪查分器",
        ),
        Subcommand(
            "key",
            Args["api_key", str],
            help_text="/bind key <落雪个人 API 密钥> 绑定个人 API 密钥（自动绑定好友码）",
        ),
        Subcommand("divingfish", help_text="/bind divingfish 绑定水鱼查分器"),
        Args["api_key?", str],
        meta=CommandMeta("[查分器相关]绑定游戏账号/查分器"),
    ),
    priority=10,
    block=True,
    skip_for_unmatch=False,
    extensions=[_MaskSensitiveExtension],
)

alconna_import = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "import",
        Subcommand("divingfish", help_text="导入成绩到水鱼查分器"),
        Subcommand("lxns", help_text="导入成绩到落雪查分器"),
        Subcommand("all", help_text="同时导入成绩到落雪和水鱼查分器"),
        Args["qr_code", str],
        meta=CommandMeta("[舞萌DX]导入游玩次数或同步成绩到查分器", usage="/import [divingfish|lxns|all] <qr_code>"),
    ),
    priority=10,
    block=True,
)

alconna_ticket = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "ticket",
        Args["qr_code", str],
        meta=CommandMeta("[舞萌DX]发送六倍票", usage="/ticket <qr_code>"),
    ),
    priority=10,
    block=True,
    permission=lambda: config.enable_arcade_write,
)

alconna_logout = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "logout",
        Args["qr_code", str],
        meta=CommandMeta("[舞萌DX]尝试强制登出", usage="/logout <qr_code>"),
    ),
    priority=10,
    block=True,
    permission=lambda: config.enable_arcade_write,
)

alconna_unlock = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "unlock",
        Args["qr_code", str],
        meta=CommandMeta("[舞萌DX]解锁新框紫铺", usage="/unlock <qr_code>"),
    ),
    priority=10,
    block=True,
    permission=lambda: config.enable_arcade_write,
)

alconna_unbind = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "unbind",
        Args["provider", Literal["all", "lxns", "divingfish", "maimai"]],
        meta=CommandMeta("[查分器相关]解绑查分器账号", usage="/unbind <all|lxns|divingfish|maimai>"),
    ),
    priority=10,
    block=True,
)

alconna_source = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "source",
        Args["provider", str],
        meta=CommandMeta("[查分器相关]设置默认查分器", usage="/source <lxns|divingfish>"),
    ),
    priority=10,
    block=True,
    aliases={"provider"},
)


alconna_alias = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "alias",
        Subcommand("help"),
        Subcommand(
            "add", Args["song_id", int], Args["alias", str], help_text="/alias add <乐曲ID> <别名> 添加乐曲别名"
        ),
        Subcommand("update", help_text="/alias update 更新本地乐曲别名列表"),
        Subcommand("query", Args["name", AllParam(str)], help_text="/alias query <id|别名> 查询该歌曲有什么别名"),
        meta=CommandMeta("[舞萌DX]乐曲别名管理"),
    ),
    priority=10,
    block=True,
)

alconna_plate_process = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        r"re:(.+)进度\s?(.+)?",
        meta=CommandMeta(
            "[舞萌DX]牌子进度",
            usage=".真极进度 / .熊将进度 难 / .紫舞舞进度",
        ),
    ),
    priority=10,
    block=True,
)

alconna_level_process = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        r"re:([0-9]+\+?)\s?([abcdsfxp\+]+)\s?([\u4e00-\u9fa5]+)?进度\s?([0-9]+)?\s?(.+)?",
        meta=CommandMeta(
            "[舞萌DX]等级进度",
            usage=".13+ sss 进度 / .14 ap 进度",
        ),
    ),
    priority=10,
    block=True,
)

alconna_update = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "update",
        Subcommand("songs", help_text="/update songs 更新乐曲信息数据库"),
        Subcommand("alias", help_text="/update alias 更新乐曲别名列表"),
        Subcommand("chart", help_text="/update chart 更新 music_chart.json 文件"),
        meta=CommandMeta("[舞萌DX]更新乐曲信息或别名列表"),
    ),
    priority=10,
    block=True,
)

alconna_fortune = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "fortune",
        meta=CommandMeta("[舞萌DX]获取今日上机运势"),
    ),
    aliases={"今日舞萌"},
    priority=10,
    block=True,
)

alconna_analysis = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "analysis",
        meta=CommandMeta("[舞萌DX]根据 B100 获取玩家成分"),
    ),
    aliases={"底力分析", "成分分析"},
    priority=10,
    block=True,
)

alconna_trend = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "trend",
        Args["time_range?", str],
        Args["render_mode?", Literal["simple", "detailed"]],
        meta=CommandMeta(
            "[舞萌DX]生成玩家 DX Rating 趋势（需绑定落雪查分器）",
            usage="/trend [7天|1个月] [simple|detailed]",
        ),
    ),
    priority=10,
    block=True,
)

alconna_recommend = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "recommend",
        meta=CommandMeta("[舞萌DX]生成随机推分曲目"),
    ),
    aliases={"什么推分", "推分", "推分推荐"},
    priority=10,
    block=True,
)

alconna_maistatus = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "maistatus",
        meta=CommandMeta("[舞萌DX]检测服务器状态（可选渲染状态页截图）", usage="/maistatus"),
    ),
    aliases={"舞萌状态"},
    priority=10,
    block=True,
)

alconna_netupdate = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "netupdate",
        Args["title_ver?", str],
        Subcommand("download", help_text="/netupdate download [版本] 下载全部机台更新包（管理员）"),
        meta=CommandMeta(
            "[舞萌DX]查询机台更新事件（ALL.Net 配信指示書）",
            usage="/netupdate [版本]",
        ),
    ),
    aliases={"机台更新", "更新包"},
    priority=10,
    block=True,
)

alconna_rikka = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "rikka",
        meta=CommandMeta("输出 Rikka 插件信息"),
    ),
    priority=10,
    block=True,
)

alconna_mai = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "mai",
        Subcommand("b50", help_text="[舞萌DX] 生成玩家 Best 50"),
        Subcommand("r50", help_text="[舞萌DX] 生成玩家 Recent 50 (需绑定落雪查分器)"),
        Subcommand("n50", help_text="[舞萌DX] 生成玩家基于拟合系数的 Top-50"),
        Subcommand("ap50", help_text="[舞萌DX] 生成玩家 ALL PERFECT 50"),
        Subcommand("pc50", help_text="[舞萌DX] 生成玩家游玩次数 Top-50"),
        Subcommand(
            "minfo",
            Args["name", AllParam(str)],
            help_text="[舞萌DX]获取乐曲信息 /mai minfo <id|别名>",
        ),
        Subcommand(
            "random",
            Args["filters", AllParam(str)],
            alias={"随机乐曲", "随"},
            help_text="[舞萌DX]随机抽取乐曲 /mai random [难度] [等级|定数]",
        ),
        Subcommand(
            "score",
            Args["name", AllParam(str)],
            help_text="[舞萌DX]获取单曲游玩情况 /mai score <id|别名>",
        ),
        Subcommand(
            "scorelist",
            Args["arg", AllParam(str)],
            help_text="[舞萌DX]获取指定条件的成绩列表 /mai scorelist <level|ach|diff> [page]",
        ),
        meta=CommandMeta(
            "[舞萌DX] 舞萌相关指令",
            usage="/mai help",
        ),
    ),
    priority=10,
    block=True,
)

alconna_b50 = on_alconna(
    Alconna(COMMAND_PREFIXES, "b50", meta=CommandMeta("[舞萌DX] 生成玩家 Best 50")),
    priority=10,
    block=True,
)

alconna_ap50 = on_alconna(
    Alconna(COMMAND_PREFIXES, "ap50", meta=CommandMeta("[舞萌DX] 生成玩家 ALL PERFECT 50")),
    priority=10,
    block=True,
)

alconna_r50 = on_alconna(
    Alconna(COMMAND_PREFIXES, "r50", meta=CommandMeta("[舞萌DX] 生成玩家 Recent 50 (需绑定落雪查分器)")),
    priority=10,
    block=True,
)

alconna_n50 = on_alconna(
    Alconna(COMMAND_PREFIXES, "n50", meta=CommandMeta("[舞萌DX] 生成玩家基于拟合系数的 Top-50")),
    priority=10,
    block=True,
)

alconna_pc50 = on_alconna(
    Alconna(COMMAND_PREFIXES, "pc50", meta=CommandMeta("[舞萌DX] 生成玩家游玩次数 Top-50")),
    priority=10,
    block=True,
)

alconna_minfo = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "minfo",
        Args["name", AllParam(str)],
        meta=CommandMeta("[舞萌DX]获取乐曲信息 /minfo <id|别名>"),
    ),
    priority=10,
    block=True,
)

alconna_random = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "random",
        Args["filters", AllParam(str)],
        meta=CommandMeta("[舞萌DX]随机抽取乐曲 /random [难度] [等级|定数]"),
    ),
    aliases={"随机乐曲", "随"},
    priority=10,
    block=True,
)

alconna_score = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "score",
        Args["name", AllParam(str)],
        meta=CommandMeta("[舞萌DX]获取单曲游玩情况 /score <id|别名>"),
    ),
    priority=10,
    block=True,
)

alconna_scorelist = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "scorelist",
        Args["arg", AllParam(str)],
        meta=CommandMeta("[舞萌DX]获取指定条件的成绩列表 /scorelist <level|ach|diff> [page]"),
    ),
    priority=10,
    block=True,
)

alconna_chu = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "chu",
        Subcommand(
            "b50",
            Args["target?", At],
            help_text="[中二节奏]生成玩家 Best 50，可用 @某人 查询他人",
        ),
        Subcommand(
            "r50",
            Args["target?", At],
            help_text="[中二节奏]生成玩家 Recent 50，可用 @某人 查询他人",
        ),
        Subcommand(
            "ap50",
            Args["target?", At],
            help_text="[中二节奏]生成玩家 ALL PERFECT 50，可用 @某人 查询他人",
            alias={"aj50", "aj30"},
        ),
        Subcommand(
            "minfo",
            Args["name", AllParam(str)],
            help_text="[中二节奏]获取乐曲信息 /chu minfo <id|曲名>（别名 /chu 查歌）",
            alias={"查歌"},
        ),
        Subcommand(
            "random",
            help_text="[中二节奏]随机获取一首乐曲（别名 /chu 随机）",
            alias={"随机"},
        ),
        Subcommand(
            "score",
            Args["name", AllParam(str)],
            help_text="[中二节奏]获取单曲游玩情况 /chu score <id|曲名> [@某人]",
        ),
        Subcommand(
            "scorelist",
            Args["arg", AllParam(str)],
            help_text="[中二节奏]获取指定条件的成绩列表 /chu scorelist [level] [page] [@某人]",
        ),
        Subcommand(
            "plate",
            Args["plate_id", int],
            help_text="[中二节奏]设置成绩图名片底板 /chu plate <ID>，0 为纯色",
        ),
        meta=CommandMeta(
            "[中二节奏]中二节奏统一命令头",
            usage="/chu <subcommand> [args] [@某人]",
        ),
    ),
    priority=10,
    block=True,
)

@alconna_bind.assign("lxns")
@catch_exception("尝试连接到落雪服务器时遇到错误")
async def handle_bind_lxns(
    event: Event,
    db_session: async_scoped_session,
    mai_score_provider: LXNSScoreProvider = Depends(get_lxns_provider),
    chu_score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
    friend_code: Match[str] = AlconnaMatch("friend_code"),
):
    user_id = event.get_user_id()

    if not friend_code.available:
        await _send_message(user_id, "请输入落雪好友码，例如：/bind lxns 101045113067366", finish=True)
        return

    raw = friend_code.result.strip()
    if not raw.isdigit():
        await _send_message(user_id, "好友码应为纯数字，请检查后重试。", finish=True)
        return

    player_info = None
    chu_info = None
    mai_ok = False
    chu_ok = False

    # 中二节奏：用好友码调开发者接口验证并写入绑定
    try:
        chu_info = await chu_score_provider.fetch_player_info(
            LXNSChuParams(friend_code=int(raw))
        )
        await UserBindInfoORM.set_user_chu_friend_code(db_session, user_id, raw)
        chu_ok = True
        logger.debug(f"[{user_id}] 已绑定中二好友码: {raw}")
    except Exception as e:
        logger.warning(f"[{user_id}] 中二好友码校验失败: {e}")

    # 舞萌：同一好友码通常通用，能查到就一并绑定
    try:
        player_info = await mai_score_provider.fetch_player_info(
            LXNSParams(friend_code=raw)
        )
        await UserBindInfoORM.set_user_mai_friend_code(db_session, user_id, raw)
        mai_ok = True
        logger.debug(f"[{user_id}] 已绑定舞萌好友码: {raw}")
    except Exception as e:
        logger.debug(f"[{user_id}] 舞萌好友码校验失败（可能未游玩）: {e}")

    if not (chu_ok or mai_ok):
        await _send_message(
            user_id,
            f"未能在落雪查到好友码 {raw}，请确认好友码是否正确。\n"
            "（好友码可在落雪查分器个人主页获取）",
            finish=True,
        )
        return

    # 写回「底力分析」插件的共享绑定库，两边共用同一份好友码
    player_name = ""
    for info in (chu_info, player_info):
        if info is not None and getattr(info, "name", None):
            player_name = str(info.name)
            break
    save_shared_friend_code(user_id, raw, player_name)

    games = []
    if chu_ok:
        games.append("中二节奏")
    if mai_ok:
        games.append("舞萌")
    await _send_message(
        user_id,
        f"已成功绑定落雪查分器⭐\n好友码：{raw}\n已绑定：{' / '.join(games)}",
        finish=True,
    )
    return


async def _bind_lxns_api_key(
    event: Event,
    db_session: async_scoped_session,
    chu_score_provider: LXNSChuScoreProvider,
    key: str,
) -> None:
    user_id = event.get_user_id()
    key = key.strip()

    # 先验证密钥有效，避免把错密钥写进库
    try:
        info = await chu_score_provider.fetch_player_info_by_user_token(key)
    except Exception as e:
        logger.warning(f"[{user_id}] 落雪个人 API 密钥校验失败: {e}")
        await _send_message(user_id, "密钥校验失败，请检查后重试。", finish=True)
        return

    await UserBindInfoORM.set_lxns_api_key(db_session, user_id, key)

    # 个人密钥的用户接口会返回好友码，顺手绑定，省去用户再发 /bind lxns。
    # 已有好友码时保留原值，避免覆盖用户此前的绑定。
    bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    if not bind_info or not bind_info.chu_friend_code:
        await UserBindInfoORM.set_user_chu_friend_code(db_session, user_id, str(info.friend_code))
        friend_code_tip = f"已自动绑定中二好友码：{info.friend_code}\n"
    else:
        friend_code_tip = ""

    # 同步给底力分析插件：好友码 + 个人密钥，让它也能用全部成绩进行分析。
    effective_code = (
        bind_info.chu_friend_code
        if (bind_info and bind_info.chu_friend_code)
        else str(info.friend_code)
    )
    save_shared_friend_code(user_id, str(effective_code), info.name)
    save_shared_api_key(user_id, key)

    await _send_message(
        user_id,
        f"已补充绑定落雪个人 API 密钥⭐\n玩家：{info.name}\n"
        f"{friend_code_tip}"
        "现在 AP50 / 成绩列表可以显示具体分数了，底力分析也会改用全部成绩。",
        finish=True,
    )


@alconna_bind.assign("key")
@catch_exception("绑定落雪个人 API 密钥时遇到错误")
async def handle_bind_key(
    event: Event,
    db_session: async_scoped_session,
    chu_score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
    api_key: Match[str] = AlconnaMatch("api_key"),
):
    """补充绑定落雪个人 API 密钥。

    好友码只能走开发者接口（B50 / R50 有分数，其余只有评级）。个人密钥走
    用户接口，能拿到全部成绩的具体分数，供 AP50 / scorelist 显示。
    """
    user_id = event.get_user_id()

    if not api_key.available or not api_key.result.strip():
        await _send_message(
            user_id,
            "请输入落雪个人 API 密钥，例如：/bind key <密钥>\n"
            "（可在落雪咖啡屋个人账号设置里获取）",
            finish=True,
        )
        return

    await _bind_lxns_api_key(event, db_session, chu_score_provider, api_key.result)


@alconna_bind.assign("divingfish")
async def handle_bind_divingfish(
    db_session: async_scoped_session,
    event: Event,
    divingfish_provider: DivingFishScoreProvider = Depends(get_divingfish_provider),
):
    user_id = event.get_user_id()

    await divingfish_provider.start_binding(user_id)
    player_info = await divingfish_provider.fetch_player_info(DivingFishParams(user_id))

    await UserBindInfoORM.set_diving_fish_as_binded(db_session, user_id)

    await _send_message(user_id, f"已绑定至水鱼账号: {player_info.name}", finish=True)


@alconna_bind.assign("help")
async def handle_bind_help(event: Event):
    user_id = event.get_user_id()

    help_text = (
        "查分器绑定帮助:\n"
        "/bind lxns <落雪好友码> 绑定落雪查分器\n"
        "/bind key <落雪个人 API 密钥> 补充绑定（显示具体分数）\n"
        "/bind divingfish 绑定水鱼查分器\n"
    )

    await _send_message(user_id, help_text, finish=True)


@alconna_bind.assign("$main")
async def handle_bind_main(
    event: Event,
    db_session: async_scoped_session,
    chu_score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
    api_key: Match[str] = AlconnaMatch("api_key"),
):
    user_id = event.get_user_id()

    # 兼容 /bind <密钥> 的写法（漏写 key 子命令）
    if api_key.available and api_key.result.strip():
        await _bind_lxns_api_key(event, db_session, chu_score_provider, api_key.result)
        return

    user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    lxns_binded = False
    divingfish_binded = False
    default_source = "未设置"
    if user_bind_info:
        if user_bind_info.lxns_api_key and user_bind_info.mai_friend_code:
            lxns_binded = True
        if user_bind_info.diving_fish_oauth:
            divingfish_binded = True
        if user_bind_info.default_provider:
            default_source = "落雪" if user_bind_info.default_provider == "lxns" else "水鱼"

    lxns = "已绑定" if lxns_binded else "未绑定"
    divingfish = "已绑定" if divingfish_binded else "未绑定"
    chu_binded = bool(user_bind_info and user_bind_info.chu_friend_code)
    chu_status = f"已绑定 ({user_bind_info.chu_friend_code})" if user_bind_info and chu_binded else "未绑定"
    user_bind_detail = (
        f"查分器绑定情况:\n"
        f"- 落雪查分器: {lxns}\n"
        f" - 水鱼查分器: {divingfish}\n"
        f" - 中二节奏: {chu_status}\n\n"
        f"当前默认查分源: {default_source}"
    )

    await _send_message(user_id, user_bind_detail)

    return await handle_bind_help(event)


@alconna_import.assign("$main")
@catch_exception("导入游玩次数失败")
async def handle_import_play_count(
    event: Event,
    db_session: async_scoped_session,
    qr_code: Match[str] = AlconnaMatch("qr_code"),
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .import <qr_code>", finish=True)
        return

    workflow_result = await run_extend_score_workflow(qr_code.result)

    records: list[tuple[int, int, int]] = []
    for item in workflow_result:
        if not isinstance(item, dict):
            continue
        try:
            song_id = int(item["musicId"])
            difficulty = int(item["level"])
            play_count = int(item["playCount"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append((song_id, difficulty, play_count))

    if not records:
        await _send_message(user_id, "未获取到可用的游玩次数数据", finish=True)
        return

    imported = await MaiPlayCountORM.upsert_user_play_counts(db_session, user_id, records)

    await _send_message(user_id, f"已导入 {imported} 条游玩次数记录", finish=True)


@alconna_import.assign("divingfish")
@catch_exception("更新水鱼查分器失败")
async def handle_import_divingfish(
    event: Event,
    db_session: async_scoped_session,
    qr_code: Match[str] = AlconnaMatch("qr_code"),
    divingfish_provider: DivingFishScoreProvider = Depends(get_divingfish_provider),
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .import <qr_code>", finish=True)
        return

    # 检查是否绑定了水鱼
    bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    if not bind_info or bind_info.diving_fish_oauth:
        await _send_message(user_id, "未绑定水鱼查分器，请使用 `.bind divingfish` 进行绑定", finish=True)
        return

    identifier = DivingFishParams(user_id=event.get_user_id())

    workflow_result = await run_extend_score_workflow(qr_code.result)
    if not workflow_result:
        await _send_message(user_id, "未获取到可用的游玩次数数据", finish=True)
        return

    divingfish_scores = await divingfish_provider.convert_to_diving_fish_format(workflow_result)
    await divingfish_provider.upload_to_diving_fish(identifier, divingfish_scores)

    # 同时更新本地游玩次数数据库
    records: list[tuple[int, int, int]] = []
    for item in workflow_result:
        if not isinstance(item, dict):
            continue
        try:
            song_id = int(item["musicId"])
            difficulty = int(item["level"])
            play_count = int(item["playCount"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append((song_id, difficulty, play_count))
    imported = await MaiPlayCountORM.upsert_user_play_counts(db_session, user_id, records)

    await _send_message(user_id, f"水鱼查分器更新成功！共更新了 {imported} 条记录", finish=True)


@alconna_import.assign("lxns")
@catch_exception()
async def handle_import_lxns(
    event: Event, db_session: async_scoped_session, qr_code: Match[str] = AlconnaMatch("qr_code")
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .import <qr_code>", finish=True)
        return

    # 检查是否绑定了落雪查分器
    bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    if not bind_info or not bind_info.lxns_api_key:
        await _send_message(user_id, "未绑定落雪个人 API 密钥，请使用 /bind key <密钥> 命令绑定", finish=True)
        return
    user_token = bind_info.lxns_api_key

    try:
        all_scores = await run_extend_score_workflow(qr_code.result)
    except RuntimeError as e:
        logger.error(f"登录失败: {e}")
        await _send_message(user_id, f"登录失败: {e}", finish=True)
        return

    if not all_scores:
        await _send_message(user_id, "未获取到可用的游玩次数数据", finish=True)
        return
    logger.debug(f"获取到的成绩数量: {len(all_scores)}")

    updated_scores = await get_updated_score(all_scores, user_token)  # type: ignore
    logger.debug(f"需要更新的成绩数量: {len(updated_scores)}")
    if not updated_scores:
        await _send_message(user_id, "没有需要更新的成绩", finish=True)
        return

    logger.debug("尝试上传至落雪服务器...")
    upload_scores = convert_to_lxns_maimai_format(updated_scores)
    await upload_to_lxns_maimai(user_token, upload_scores)

    # 同时更新本地游玩次数数据库
    records: list[tuple[int, int, int]] = []
    for item in all_scores:
        if not isinstance(item, dict):
            continue
        try:
            song_id = int(item["musicId"])
            difficulty = int(item["level"])
            play_count = int(item["playCount"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append((song_id, difficulty, play_count))
    await MaiPlayCountORM.upsert_user_play_counts(db_session, user_id, records)

    await _send_message(user_id, f"落雪查分器更新成功！共更新了 {len(updated_scores)} 条记录", finish=True)


@alconna_import.assign("all")
@catch_exception()
async def handle_import_all(
    event: Event,
    db_session: async_scoped_session,
    qr_code: Match[str] = AlconnaMatch("qr_code"),
    divingfish_provider: DivingFishScoreProvider = Depends(get_divingfish_provider),
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .import <qr_code>", finish=True)
        return

    # 检查是否绑定了落雪查分器和水鱼查分器
    bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    if not bind_info or not bind_info.lxns_api_key or not bind_info.diving_fish_oauth:
        await _send_message(user_id, "未同时绑定落雪查分器，请使用 .bind 命令绑定", finish=True)
        return
    lxns_user_token = bind_info.lxns_api_key
    divingfish_param = DivingFishParams(user_id=user_id)

    all_scores = await run_extend_score_workflow(qr_code.result)
    if not all_scores:
        await _send_message(user_id, "未获取到可用的游玩次数数据", finish=True)
        return
    logger.debug(f"获取到的成绩数量: {len(all_scores)}")

    # divingfish
    logger.debug("更新水鱼查分器...")
    divingfish_scores = await divingfish_provider.convert_to_diving_fish_format(all_scores)
    await divingfish_provider.upload_to_diving_fish(divingfish_param, divingfish_scores)

    # lxns
    logger.debug("更新落雪查分器...")
    updated_scores = await get_updated_score(all_scores, lxns_user_token)  # type: ignore
    logger.debug(f"更新的成绩数量: {len(all_scores) - len(updated_scores)}")
    lxns_scores = convert_to_lxns_maimai_format(updated_scores)
    await upload_to_lxns_maimai(lxns_user_token, lxns_scores)

    # 同时更新本地游玩次数数据库
    records: list[tuple[int, int, int]] = []
    for item in all_scores:
        if not isinstance(item, dict):
            continue
        try:
            song_id = int(item["musicId"])
            difficulty = int(item["level"])
            play_count = int(item["playCount"])
        except (KeyError, TypeError, ValueError):
            continue
        records.append((song_id, difficulty, play_count))
    imported = await MaiPlayCountORM.upsert_user_play_counts(db_session, user_id, records)

    await _send_message(user_id, f"查分器更新成功！共更新了 {imported} 条记录", finish=True)


@alconna_ticket.handle()
@catch_exception("发送倍票失败")
async def handle_ticket(
    event: Event,
    qr_code: Match[str] = AlconnaMatch("qr_code"),
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .ticket <qr_code>", finish=True)
        return

    await run_extend_ticket_workflow(qr_code.result)

    await _send_message(user_id, "已成功发送了 6 倍票", finish=True)


@alconna_logout.handle()
@catch_exception(reply_prefix="强制登出失败")
async def handle_logout(
    event: Event,
    qr_code: Match[str] = AlconnaMatch("qr_code"),
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .logout <qr_code>", finish=True)
        return

    await run_extent_force_logout(qr_code.result)
    await _send_message(user_id, "已尝试强制登出，请尝试重新登录", finish=True)


@alconna_unlock.handle()
@catch_exception()
async def handle_rikka_unlock(
    event: Event,
    qr_code: Match[str] = AlconnaMatch("qr_code"),
):
    user_id = event.get_user_id()

    if not config.enable_arcade_provider:
        await _send_message(user_id, "机台源不可用，无法执行该操作", finish=True)
        return

    if not qr_code.available or not qr_code.result:
        await _send_message(user_id, "请提供二维码内容: .unlock <qr_code>", finish=True)
        return

    await run_unlock_workflow(qr_code.result)
    await _send_message(user_id, "解锁成功！", finish=True)


@alconna_unbind.handle()
async def handle_unbind(
    event: Event,
    db_session: async_scoped_session,
    provider: Match[Literal["all", "lxns", "divingfish", "maimai"]] = AlconnaMatch("provider"),
):
    user_id = event.get_user_id()

    if not provider.available or provider.result not in ["all", "lxns", "divingfish", "maimai"]:
        await _send_message(user_id, "请输入有效的查分器名称: all, lxns, divingfish 或 maimai", finish=True)
        return

    provider_name = provider.result if provider.result != "all" else None

    try:
        await UserBindInfoORM.unset_user_bind_info(db_session, user_id, provider_name)
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    # 解绑落雪时一并清掉底力分析的共享绑定，避免那边还能继续查询。
    if provider.result in ("lxns", "all"):
        delete_shared_binding(user_id)

    await _send_message(user_id, f"已解绑查分器: {provider.result}", finish=True)


@alconna_source.handle()
async def handle_source(
    event: Event,
    db_session: async_scoped_session,
    provider: Match[str] = AlconnaMatch("provider"),
):
    user_id = event.get_user_id()

    if not provider.available or provider.result not in ["lxns", "divingfish"]:
        await _send_message(user_id, "请输入有效的查分器名称: lxns 或 divingfish", finish=True)
        return

    try:
        await UserBindInfoORM.set_default_provider(db_session, user_id, provider.result)  # type: ignore
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    await _send_message(user_id, f"已将默认查分器设置为: {provider.result} ⭐", finish=True)


@alconna_b50.handle()
@alconna_mai.assign("b50")
@catch_exception()
async def handle_mai_b50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
    divingfish_provider: DivingFishScoreProvider = Depends(get_divingfish_provider),
):
    """舞萌 Best50：默认水鱼 QQ 号直查，失败再回退原有 maimai.py 链路。"""
    user_id = event.get_user_id()

    # ── 首选：水鱼 QQ 号直查（无需绑定、无需开发者密钥） ──
    bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    prefer_legacy = bool(bind_info and bind_info.default_provider == "lxns")
    if not prefer_legacy and user_id.isdigit():
        try:
            df_params = DivingFishParams(user_id=user_id, qq=user_id)
            logger.info(f"[{user_id}] 舞萌 B50 使用水鱼 QQ 直查")
            player_info = await divingfish_provider.fetch_player_info(df_params)
            player_b50 = await divingfish_provider.fetch_player_b50(df_params)
            if player_b50.standard or player_b50.dx:
                pic = await renderer.render_mai_player_best50(player_b50, player_info)
                await _send_message(user_id, "", images=[pic], finish=True)
                return
            logger.info(f"[{user_id}] 水鱼返回空 B50，回退 maimai.py 链路")
        except Exception as e:
            logger.info(f"[{user_id}] 水鱼 QQ 直查失败({e})，回退 maimai.py 链路")

    # ── 回退：原有 maimai.py 链路（落雪等 provider） ──
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)

    logger.info(f"[{user_id}] 获取玩家 Best50, 查分器类型: {type(provider)}")
    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")

    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家信息...")
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家 Best50...")
    player_b50 = await score_provider.fetch_player_b50(params)

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await renderer.render_mai_player_best50(player_b50, player_info)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_ap50.handle()
@alconna_mai.assign("ap50")
@catch_exception()
async def handle_mai_ap50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)

    logger.info(f"[{user_id}] 获取玩家 AP50, 查分器类型: {type(score_provider)}")
    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")

    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家信息...")
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家 AP 50...")
    if isinstance(provider, LXNSProvider):
        user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
        if user_bind_info is None or user_bind_info.lxns_api_key is None:
            await _send_message(
                event.get_user_id(),
                "你还没有绑定任何查分器喵，请先用 /bind 绑定一个查分器谢谢喵",
                finish=True,
            )
            return  # Avoid TypeError.

        identifier.credentials = user_bind_info.lxns_api_key
        params = score_provider.ParamsType(provider, identifier)

    player_ap50 = await score_provider.fetch_player_ap50(params)

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await renderer.render_mai_player_best50(player_ap50, player_info)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_r50.handle()
@alconna_mai.assign("r50")
@catch_exception()
async def handle_mai_r50(
    event: Event, db_session: async_scoped_session, score_provider: LXNSScoreProvider = Depends(get_lxns_provider)
):
    user_id = event.get_user_id()

    logger.info(f"[{user_id}] 获取玩家 Recent 50, 查分器名称: {score_provider.provider}")
    logger.debug(f"[{user_id}] 1/4 尝试从数据库中获取玩家绑定信息...")

    user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)

    new_player_friend_code = None
    if user_bind_info is None and user_id.isdigit():
        logger.warning(f"[{user_id}] 未能获取玩家码，数据库中不存在绑定的玩家数据")
        logger.debug(f"[{user_id}] 1/4 尝试通过 QQ 请求玩家数据")
        try:
            player_info = await score_provider.fetch_player_info_by_qq(user_id)
            new_player_friend_code = player_info.friend_code
        except ClientResponseError as e:
            logger.warning(f"[{user_id}] 无法通过 QQ 号请求玩家数据: {e.code}: {e.message}")

            await _send_message(
                user_id, "查询 Recent 50 的操作需要绑定落雪查分器喵，还请使用 /bind 指令进行绑定喵呜", finish=True
            )

            return

    friend_code = new_player_friend_code or user_bind_info.mai_friend_code  # type: ignore
    if not friend_code:
        logger.warning(f"[{user_id}] 无法获取好友码，无法继续查询。")
        await _send_message(user_id, "无法获取好友码，请确认已绑定或查分器可用。", finish=True)
        return
    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家信息...")
    params = score_provider.ParamsType(friend_code=friend_code)
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家 Recent 50...")
    player_r50 = await score_provider.fetch_player_r50(friend_code)

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await renderer.render_mai_player_scores(player_r50, player_info, title="Recent 50")

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_pc50.handle()
@alconna_mai.assign("pc50")
@catch_exception()
async def handle_pc50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)

    if not config.enable_arcade_provider:
        await _send_message(user_id, "管理员未启用pc数查询喵呜", finish=True)

    logger.info(f"[{user_id}] 获取玩家 PC50, 查分器类型: {type(provider)}")

    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")

    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家信息...")
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家所有成绩...")
    if isinstance(provider, LXNSProvider):
        user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
        if user_bind_info is None or user_bind_info.lxns_api_key is None:
            await _send_message(
                event.get_user_id(),
                "你还没有绑定任何查分器喵，请先用 /bind 绑定一个查分器谢谢喵",
                finish=True,
            )
            return  # Avoid TypeError.

        identifier.credentials = user_bind_info.lxns_api_key
        params = score_provider.ParamsType(provider, identifier)

    player_scores = await score_provider.fetch_player_pc50(params)

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await renderer.render_mai_player_best50(player_scores, player_info)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_n50.handle()
@alconna_mai.assign("n50")
@catch_exception()
async def handle_n50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)

    logger.info(f"[{user_id}] 获取玩家 N50, 查分器类型: {type(provider)}")
    logger.debug(f"[{user_id}] 1/5 获得用户鉴权凭证...")

    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 2/5 发起 API 请求玩家信息...")
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/5 发起 API 请求玩家所有成绩")
    if isinstance(provider, LXNSProvider):
        user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
        if user_bind_info is None or user_bind_info.lxns_api_key is None:
            await _send_message(
                event.get_user_id(),
                "你还没有绑定任何查分器喵，请先用 /bind 绑定一个查分器谢谢喵",
                finish=True,
            )
            return  # Avoid TypeError.

        identifier.credentials = user_bind_info.lxns_api_key
        params = score_provider.ParamsType(provider, identifier)
    player_scores = await score_provider.fetch_player_scoreslist(params)

    logger.debug(f"[{user_id}] 4/5 计算 N50...")
    player_n50 = get_players_n50(player_scores)
    player_info.rating = player_n50.rating

    logger.debug(f"[{user_id}] 5/5 渲染玩家数据...")
    pic = await renderer.render_mai_player_best50(player_n50, player_info, calc_song_level_value=False)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_minfo.handle()
@alconna_mai.assign("minfo")
@catch_exception()
async def handle_minfo(
    event: Event,
    db_session: async_scoped_session,
    name: Match[UniMessage] = AlconnaMatch("name"),
):
    user_id = event.get_user_id()

    if not name.available:
        await _send_message(user_id, "请输入有效的乐曲ID/名称/别名！", finish=True)

    raw_query = name.result.extract_plain_text()
    logger.info(f"[{user_id}] 查询乐曲信息, 查询内容: {raw_query}")

    logger.debug(f"[{user_id}] 1/2 通过乐曲ID/别名查询乐曲信息...")
    try:
        song = await get_maisong_by_id_or_alias(db_session, raw_query)
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    logger.debug(f"[{user_id}] 2/2 构建乐曲信息模板...")
    message, images = _build_maisong_info_message(song)

    await _send_message(user_id, message, images, finish=True)


@alconna_random.handle()
@alconna_mai.assign("random")
@catch_exception()
async def handle_random(
    event: Event,
    db_session: async_scoped_session,
    filters: Match[UniMessage] = AlconnaMatch("filters"),
):
    user_id = event.get_user_id()

    raw_filters = filters.result.extract_plain_text().strip() if filters.available else ""
    tokens = [tok for tok in raw_filters.split() if tok]

    if len(tokens) > 2:
        await _send_message(user_id, "参数过多，最多填写难度与等级/定数两个条件哦~", finish=True)
        return

    diff_value = None
    diff_name = None
    level_value = None
    level_const = None
    invalid_tokens: list[str] = []

    for tok in tokens:
        up_tok = tok.upper()
        up_tok = "REMASTER" if up_tok == "RE:MASTER" else up_tok
        if up_tok in _DIFFICULTY_VALUE_MAP:
            if diff_value is not None:
                invalid_tokens.append(tok)
                continue
            diff_value = _DIFFICULTY_VALUE_MAP[up_tok]
            diff_name = up_tok
            continue

        if re.fullmatch(r"\d+\+?", tok):
            if level_value is not None:
                invalid_tokens.append(tok)
                continue
            level_value = tok
            continue

        if "." in tok and re.fullmatch(r"\d+(?:\.\d+)?", tok):
            if level_const is not None:
                invalid_tokens.append(tok)
                continue
            try:
                level_const = float(tok)
            except ValueError:
                invalid_tokens.append(tok)
            continue

        invalid_tokens.append(tok)

    if invalid_tokens:
        await _send_message(
            user_id,
            f"以下参数无法识别: {' '.join(invalid_tokens)}，请使用难度(BASIC~RE:MASTER)、等级(如12+)或定数(如12.7)重试~",
            finish=True,
        )
        return

    logger.info(f"[{user_id}] 随机抽取乐曲, 条件 diff={diff_name}, level={level_value}, const={level_const}")

    song_ids = await MaiSongORM.get_all_song_ids(db_session)
    songs = await MaiSongORM.get_songs_info_by_ids(db_session, list(song_ids))

    def match_song(song: MaiSong) -> bool:
        candidates = list(song.difficulties.standard) + list(song.difficulties.dx)
        for d in candidates:
            if diff_value is not None and d.difficulty != diff_value:
                continue
            if level_value and d.level != level_value:
                continue
            if level_const is not None and abs(d.level_value - level_const) > 1e-6:
                continue
            return True
        return False

    filtered_songs = [s for s in songs if match_song(s)]

    if not filtered_songs:
        await _send_message(user_id, "未找到符合条件的乐曲喵，试试放宽条件再来一次吧~", finish=True)
        return

    song = random.choice(filtered_songs)
    message, images = _build_maisong_info_message(song)

    await _send_message(user_id, message, images, finish=True)


@alconna_alias.assign("update")
async def handle_alias_update(
    event: Event,
    db_session: async_scoped_session,
):
    user_id = event.get_user_id()
    nb_config = get_driver().config

    if user_id not in nb_config.superusers:
        await _send_message(event.get_user_id(), "更新乐曲别名需要管理员权限哦", finish=True)

    logger.info(f"[{user_id}] 更新乐曲别名列表")

    await update_song_alias_list(db_session)

    logger.info(f"[{user_id}] 乐曲别名列表更新完成")

    await _send_message(user_id, "乐曲别名列表已更新完成 ⭐", finish=True)


@alconna_alias.assign("add")
async def handle_alias_add(
    event: Event,
    db_session: async_scoped_session,
    name: Match[str] = AlconnaMatch("alias"),
    song_id: Match[int] = AlconnaMatch("song_id"),
):
    user_id = event.get_user_id()

    if config.add_alias_need_admin:
        nb_config = get_driver().config

        if user_id not in nb_config.superusers:
            await _send_message(event.get_user_id(), "更新乐曲别名需要管理员权限哦", finish=True)

    logger.info(f"[{user_id}] 添加乐曲别名, 乐曲ID: {song_id.result}, 别名: {name.result}")

    await MaiSongAliasORM.add_custom_alias(db_session, song_id.result, name.result)

    await _send_message(user_id, f"已为乐曲 ID {song_id.result} 添加别名: {name.result} ⭐", finish=True)


@alconna_alias.assign("query")
async def handle_alias_query(
    event: Event,
    db_session: async_scoped_session,
    name: Match[UniMessage] = AlconnaMatch("name"),
):
    user_id = event.get_user_id()

    if not name.available:
        await _send_message(user_id, "请输入有效的乐曲ID/名称/别名！", finish=True)

    raw_query = name.result.extract_plain_text()

    logger.info(f"[{user_id}] 查询乐曲别名, 查询内容: {raw_query}")

    logger.debug(f"[{user_id}] 1/4 通过乐曲ID/别名查询乐曲信息...")
    try:
        song = await get_maisong_by_id_or_alias(db_session, raw_query)
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    logger.debug(f"[{user_id}] 2/4 获取乐曲别名列表...")
    aliases = await MaiSongAliasORM.get_aliases(db_session, song.id)

    logger.debug(f"[{user_id}] 3/4 构建乐曲别名模板...")

    if not aliases:
        await _send_message(user_id, f"乐曲 ID {song.id} ('{song.title}') 暂无别名记录！", finish=True)
        return

    alias_list_content = ", ".join(aliases)
    await _send_message(
        user_id, f"乐曲 ID {song.id} ('{song.title}') 的别名列表如下：\n{alias_list_content}", finish=True
    )


@alconna_alias.assign("help")
async def handle_alias_help(event: Event):
    user_id = event.get_user_id()

    help_text = (
        "乐曲别名管理帮助:\n"
        "/alias add <乐曲ID> <别名> 添加乐曲别名\n"
        "/alias update 更新本地乐曲别名列表\n"
        "/alias query <id|别名> 查询该歌曲有什么别名\n"
    )

    await _send_message(user_id, help_text, finish=True)


@alconna_alias.assign("$main")
async def handle_alias_main(event: Event):
    return await handle_alias_help(event)


@alconna_score.handle()
@alconna_mai.assign("score")
@catch_exception()
async def handle_score(
    event: Event,
    db_session: async_scoped_session,
    name: Match[UniMessage] = AlconnaMatch("name"),
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()

    if not name.available:
        await _send_message(user_id, "请输入有效的乐曲ID/名称/别名！", finish=True)

    raw_query = name.result.extract_plain_text()
    logger.info(f"[{user_id}] 查询单曲游玩情况, 查询内容: {raw_query}")

    logger.debug(f"[{user_id}] 1/5 通过乐曲ID/别名查询乐曲信息...")
    try:
        song = await get_maisong_by_id_or_alias(db_session, raw_query)
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    logger.debug(f"[{user_id}] 2/5 推断获取的是 DX 铺面还是标准铺面")
    if raw_query.isdigit():
        if int(raw_query) > 10000 or not song.difficulties.standard:
            is_dx = True
        else:
            is_dx = False
    else:
        is_dx = len(song.difficulties.dx) > 0
    logger.debug(f"[{user_id}] 2/5 推断为 {'DX' if is_dx else '标准'} 铺面")

    logger.debug(f"[{user_id}] 3/5 获得用户鉴权凭证...")
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)
    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 4/5 发起 API 请求玩家信息...")
    scores = await score_provider.fetch_player_minfo(params, song.id, "dx" if is_dx else "standard")

    if not scores:
        await _send_message(user_id, f"未找到乐曲 '{song.title}' 的游玩记录喵~不如先去挑战一下吧~", finish=True)
        return

    logger.debug(f"[{user_id}] 5/5 渲染玩家数据...")
    pic = await renderer.render_mai_player_song_info(song, scores)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_scorelist.handle()
@alconna_mai.assign("scorelist")
@catch_exception()
async def handle_scorelist(
    event: Event,
    db_session: async_scoped_session,
    arg: Match[UniMessage] = AlconnaMatch("arg"),
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()

    if not arg.available or not arg.result:
        await _send_message(
            user_id,
            "/scorelist 使用帮助\n"
            "/scorelist <level> 获取指定等级的成绩列表\n"
            "/scorelist <level_value> 获取指定等级（定数）的成绩列表\n"
            "/scorelist ach<float> 获取指定达成率的成绩列表\n"
            "/scorelist <diff> 获取指定铺面难度的成绩列表\n"
            "/scorelist <条件> [page] 获取指定页（每页 50 条）\n"
            "eg.\n"
            "/scorelist 12+\n"
            "/scorelist 12.7\n"
            "/scorelist ach100.8\n"
            "/scorelist expert\n"
            "/scorelist ach100.8 2",
            finish=True,
        )

    raw_input = arg.result.extract_plain_text().strip()
    query_parts = raw_input.split()

    if not query_parts:
        await _send_message(user_id, "命令格式错误，请检查后重新输入！", finish=True)
        return

    raw_query = query_parts[0]
    page = 1
    if len(query_parts) >= 2:
        if not query_parts[1].isdigit() or int(query_parts[1]) <= 0:
            await _send_message(user_id, "页码必须是大于 0 的整数", finish=True)
            return
        page = int(query_parts[1])

    if len(query_parts) > 2:
        await _send_message(user_id, "命令格式错误，请检查后重新输入！", finish=True)
        return

    logger.info(f"[{user_id}] 查询指定条件的成绩列表, 查询内容: {raw_query}")
    level: Optional[str] = None
    level_value: Optional[float] = None
    ach: Optional[float] = None
    diff: Optional[str] = None
    if raw_query.isdigit() or (raw_query.endswith("+") and raw_query[:-1].isdigit()):
        level = raw_query
        title = f"{level} 成绩列表"
    elif is_float(raw_query):
        level_value = float(raw_query)
        title = f"{level_value} 成绩列表"
    elif raw_query.startswith("ach") and is_float(raw_query[3:]):
        ach = float(raw_query[3:])
        title = f"达成率 {ach} 成绩列表"
    elif raw_query.upper() in ["BASIC", "ADVANCED", "EXPERT", "MASTER", "REMASTER", "RE:MASTER"]:
        diff = raw_query.upper()
        diff = "REMASTER" if diff == "RE:MASTER" else diff
        title = f"铺面等级 {diff} 成绩列表"
    else:
        await _send_message(user_id, "命令格式错误，请检查后重新输入！", finish=True)
        return

    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)
    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(
        db_session, user_id, provider, use_personal_api=True
    )
    params = score_provider.ParamsType(provider, identifier)
    logger.debug(f"[{user_id}] 1/4 鉴权参数: {params}")

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家信息...")
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家全部成绩...")
    scores = await score_provider.fetch_player_scoreslist(params, level, level_value, ach, diff)  # type: ignore

    if not scores:
        await _send_message(user_id, "呜呜，未找到符合条件的游玩记录喵", finish=True)
        return

    page_size = 50
    total_pages = (len(scores) + page_size - 1) // page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_scores = scores[start:end]

    if not page_scores:
        await _send_message(
            user_id, f"页码超出范围，当前共 {total_pages} 页，请输入 1~{total_pages} 之间的页码", finish=True
        )
        return

    title = f"{title} - 第 {page}/{total_pages} 页"

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await renderer.render_mai_player_scores(page_scores, player_info, title)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_mai.assign("$main")
async def handle_unified_mai_main(event: Event):
    # 裸 /mai 没有子命令时提示去看索引帮助
    await _send_message(event.get_user_id(), "发送 /help mai 查看舞萌全部指令。", finish=True)


@alconna_plate_process.handle()
@catch_exception()
async def handle_plate_process(
    event: Event,
    db_session: async_scoped_session,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()

    raw_text = event.get_plaintext().strip()
    for p in COMMAND_PREFIXES:
        if raw_text.startswith(p):
            raw_text = raw_text[len(p) :]
            break

    m = re.match(r"^(.+)进度\s?(.+)?$", raw_text)
    if not m or len(m.group(1)) < 2:
        await _send_message(user_id, "命令解析失败，请检查输入格式", finish=True)
        return

    plate_name = m.group(1)
    extra_text = m.group(2) or ""
    use_difficult = "难" in extra_text

    if plate_name == "真将":
        await _send_message(user_id, "真系没有真将哦", finish=True)
        return

    logger.info(f"[{user_id}] 查询牌子进度: {plate_name} {'难' if use_difficult else ''}")

    logger.debug(f"[{user_id}] 1/2 获得用户鉴权凭证...")
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)
    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(
        db_session, user_id, provider, use_personal_api=True
    )

    logger.debug(f"[{user_id}] 2/2 查询牌子进度...")
    try:
        plate = await maimai_client.plates(identifier, plate_name, provider)
    except InvalidPlateError:
        await _send_message(user_id, f"无效的牌子: {plate_name}", finish=True)
        return
    except Exception as e:
        await _send_message(user_id, f"查询牌子进度失败: {e}", finish=True)
        return

    remained = await plate.get_remained()
    cleared_count = await plate.count_cleared()
    total_count = await plate.count_all()

    diff_names = ["Basic", "Advanced", "Expert", "Master", "Re:MASTER"]
    level_index_to_diff = {
        LevelIndex.BASIC: 0,
        LevelIndex.ADVANCED: 1,
        LevelIndex.EXPERT: 2,
        LevelIndex.MASTER: 3,
        LevelIndex.ReMASTER: 4,
    }

    counts_by_diff = [0, 0, 0, 0, 0]
    unfinished_items = []

    for obj in remained:
        for level_idx in obj.levels:
            diff_idx = level_index_to_diff.get(level_idx, 0)
            counts_by_diff[diff_idx] += 1
            diff = obj.song.get_difficulty(plate._major_type, level_idx)
            level_value = diff.level_value if diff else 0.0
            song_type = "DX" if plate._major_type == MaimaiPySongType.DX else "SD"
            unfinished_items.append((obj.song.title, diff_idx, level_value, song_type))

    if use_difficult:
        unfinished_items = [item for item in unfinished_items if item[2] > 13.6]

    unfinished_items.sort(key=lambda x: x[2], reverse=True)

    total_left = len(unfinished_items) if use_difficult else (total_count - cleared_count)
    percentage = f"{cleared_count / total_count * 100:.2f}%" if total_left > 0 else "已完成"

    text = (
        f"{plate_name}进度 - {percentage}\n"
        f"铺面总数: {total_count}\n"
        f"剩余进度: 绿铺：{counts_by_diff[0]}首; 黄铺: {counts_by_diff[1]}首; "
        f"红铺: {counts_by_diff[2]}首; 紫铺: {counts_by_diff[3]}首; 白铺: {counts_by_diff[4]}首\n"
        f"预计剩余时间: {total_left * 2 / 60:.1f} 小时（单刷预计 {(total_left + 2) // 3}pc; 拼机预计 {(total_left + 3) // 4}pc）\n"
        f"未完成铺面列表：\n"
    )

    for title, diff_idx, level_value, song_type in unfinished_items[:10]:
        text += f"[{diff_names[diff_idx]} {level_value}] {title}[{song_type}]\n"

    await _send_message(user_id, text.strip(), finish=True)


@alconna_level_process.handle()
@catch_exception()
async def handle_level_process(
    event: Event,
    db_session: async_scoped_session,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()

    raw_text = event.get_plaintext().strip()
    for p in COMMAND_PREFIXES:
        if raw_text.startswith(p):
            raw_text = raw_text[len(p) :]
            break

    m = re.match(
        r"^([0-9]+\+?)\s?([abcdsfxp\+A-Za-z]+|[\u4e00-\u9fa5]+)\s?([\u4e00-\u9fa5]+)?进度\s?([0-9]+)?\s?(.+)?$",
        raw_text,
    )
    if not m:
        await _send_message(user_id, "命令解析失败，请检查输入格式", finish=True)
        return

    raw_level = m.group(1)
    raw_plan = m.group(2)

    plan_map = {"极": "FC", "極": "FC", "神": "AP", "将": "SSS"}
    raw_plan = plan_map.get(raw_plan, raw_plan)

    logger.info(f"[{user_id}] 查询等级进度: {raw_level} {raw_plan}")

    logger.debug(f"[{user_id}] 1/3 获得用户鉴权凭证...")
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)
    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(
        db_session, user_id, provider, use_personal_api=True
    )

    songs = list(MaiSongORM._cache.values())
    if not songs:
        await MaiSongORM.refresh_cache(db_session)
        songs = list(MaiSongORM._cache.values())

    logger.debug(f"[{user_id}] 2/3 发起 API 请求玩家所有成绩")
    scores = await score_provider.fetch_player_scoreslist(
        MaimaiPyParams(score_provider=provider, identifier=identifier)
    )
    try:
        data = get_level_process_data(songs, scores, raw_level, raw_plan)
    except ProcessDataError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    diff_names = ["Basic", "Advanced", "Expert", "Master", "Re:MASTER"]

    total_left = data.counts["unfinished"] + data.counts["not_played"]
    total_count = data.counts["total"]
    percentage = f"{data.counts['completed'] / total_count * 100:.2f}%" if total_left > 0 else "已完成"

    unfinished_items = []
    counts_by_diff = [0, 0, 0, 0, 0]

    for song, _, diff_idx in data.unfinished:
        counts_by_diff[diff_idx] += 1
        level_val = 0.0
        is_dx = False
        for d in song.difficulties.standard:
            if d.difficulty == diff_idx and d.level == raw_level:
                level_val = d.level_value
        for d in song.difficulties.dx:
            if d.difficulty == diff_idx and d.level == raw_level:
                level_val = d.level_value
                is_dx = True
        unfinished_items.append((song, diff_idx, level_val, is_dx))

    for song, diff_idx in data.not_played:
        counts_by_diff[diff_idx] += 1
        level_val = 0.0
        is_dx = False
        for d in song.difficulties.standard:
            if d.difficulty == diff_idx and d.level == raw_level:
                level_val = d.level_value
        for d in song.difficulties.dx:
            if d.difficulty == diff_idx and d.level == raw_level:
                level_val = d.level_value
                is_dx = True
        unfinished_items.append((song, diff_idx, level_val, is_dx))

    unfinished_items.sort(key=lambda x: x[2], reverse=True)

    text = (
        f"{raw_level}{raw_plan}进度 - {percentage}\n"
        f"铺面总数: {total_count}\n"
        f"剩余进度: 绿铺: {counts_by_diff[0]}首; 黄铺: {counts_by_diff[1]}首; "
        f"红铺: {counts_by_diff[2]}首; 紫铺: {counts_by_diff[3]}首; 白铺: {counts_by_diff[4]}首\n"
        f"预计剩余时间: {total_left * 2 / 60:.1f} 小时（单刷预计 {(total_left + 2) // 3}pc; 拼机预计 {(total_left + 3) // 4}pc）\n"
        f"未完成铺面列表: \n"
    )

    for song, diff_idx, level_val, is_dx in unfinished_items[:10]:
        song_type = "DX" if is_dx else "SD"
        text += f"[{diff_names[diff_idx]} {level_val}] {song.title}[{song_type}]\n"

    await _send_message(user_id, text.strip(), finish=True)


@alconna_update.assign("songs")
@catch_exception()
async def handle_update_songs(
    event: Event,
    db_session: async_scoped_session,
):
    user_id = event.get_user_id()
    nb_config = get_driver().config

    if user_id not in nb_config.superusers:
        await _send_message(event.get_user_id(), "更新乐曲信息需要管理员权限哦", finish=True)

    logger.info(f"[{user_id}] 更新乐曲信息数据库")

    mai_updated_count = await update_maimai_song_database(db_session)
    chu_updated_count = await update_chu_song_database(db_session)
    updated_count = mai_updated_count + chu_updated_count

    msg = f"乐曲信息数据库已更新完成，共更新 {updated_count}(Maimai: {mai_updated_count}, Chunithm: {chu_updated_count}) 首乐曲 ⭐"

    logger.info(f"[{user_id}] {msg}")

    await _send_message(user_id, msg, finish=True)


@alconna_update.assign("alias")
async def handle_update_aliases(
    event: Event,
    db_session: async_scoped_session,
):
    await handle_alias_update(event, db_session)


@alconna_update.assign("chart")
async def handle_update_chart(event: Event):
    await update_local_chart_file()

    await _send_message(event.get_user_id(), "music_chart.json 文件已更新完成⭐", finish=True)


@alconna_update.assign("$main")
async def handle_update_main(event: Event, db_session: async_scoped_session):
    user_id = event.get_user_id()
    nb_config = get_driver().config

    if user_id not in nb_config.superusers:
        await _send_message(event.get_user_id(), "更新乐曲信息需要管理员权限哦", finish=True)

    logger.info(f"[{event.get_user_id()}] 未提供 update 参数，默认执行全量更新")

    logger.debug(f"[{event.get_user_id()}] 1/3 更新 music_chart.json 文件")
    await update_local_chart_file()
    logger.debug(f"[{event.get_user_id()}] 2/3 更新乐曲数据库")
    mai_updated_count = await update_maimai_song_database(db_session)
    chu_updated_count = await update_chu_song_database(db_session)
    updated_count = mai_updated_count + chu_updated_count
    logger.debug(f"[{event.get_user_id()}] 3/3 更新数据库中的乐曲别名列表")
    await update_song_alias_list(db_session)

    logger.info(f"[{event.get_user_id()}] 全量更新完成，共更新 {updated_count} 首乐曲")

    await _send_message(event.get_user_id(), f"全量更新已完成，共更新 {updated_count} 首乐曲 ⭐", finish=True)


@alconna_fortune.handle()
async def handle_fortune(
    event: Event,
):
    user_id = event.get_user_id()

    logger.info(f"[{user_id}] 获取今日舞萌运势")

    fortune_message = await generate_today_fortune(user_id)

    await _send_message(
        user_id,
        fortune_message.extract_plain_text(),
        images=[Path(image.path).read_bytes() for image in fortune_message[UniImage] if image.path],
        finish=True,
    )


@alconna_analysis.handle()
@catch_exception()
async def handle_analysis(
    db_session: async_scoped_session,
    event: Event,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()

    if not SONG_TAGS_DATA_AVAILABLE:
        await _send_message(user_id, "管理员未配置乐曲标签，无法使用此功能喵", finish=True)
        return

    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)

    logger.info(f"[{user_id}] 获取玩家底力表, 查分器类型: {type(provider)}")
    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")

    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家所有成绩")
    if isinstance(provider, LXNSProvider):
        user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
        if user_bind_info is None or user_bind_info.lxns_api_key is None:
            await _send_message(
                event.get_user_id(),
                "你还没有绑定任何查分器喵，请先用 /bind 绑定一个查分器谢谢喵",
                finish=True,
            )
            return  # Avoid TypeError.

        identifier.credentials = user_bind_info.lxns_api_key
        params = score_provider.ParamsType(provider, identifier)
    player_scores = await score_provider.fetch_player_scoreslist(params)

    logger.debug(f"[{user_id}] 3/4 计算玩家成分")
    player_scores.sort(key=lambda x: x.dx_rating, reverse=True)
    player_scores = player_scores[:100]
    player_strength = get_player_strength(player_scores)

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = draw_player_strength_analysis(player_strength)
    byte = image_to_bytes(pic)

    await _send_message(user_id, "", images=[byte], finish=True)


@alconna_trend.handle()
@catch_exception()
async def handle_trend(
    db_session: async_scoped_session,
    event: Event,
    score_provider: LXNSScoreProvider = Depends(get_lxns_provider),
    time_range: Match[str] = AlconnaMatch("time_range"),
    raw_render_mode: Match[str] = AlconnaMatch("render_mode"),
):
    user_id = event.get_user_id()
    raw_time_range = time_range.result.strip() if time_range.available else ""
    render_mode = raw_render_mode.result.strip() if raw_render_mode.available else "simple"

    range_days: int | None = None
    range_desc: str | None = None

    if raw_time_range:
        parsed_days, parsed_range_desc = _parse_trend_time_window(raw_time_range)
        if parsed_days is not None:
            range_days = parsed_days
            range_desc = parsed_range_desc
        else:
            await _send_message(
                user_id,
                "时间范围参数格式错误，请使用如 `.trend 7天` 或 `.trend 1个月`（不支持 `1 个月` 这种带空格写法）",
                finish=True,
            )
            return

    if render_mode not in ["simple", "detailed"]:
        await _send_message(user_id, "渲染模式参数格式错误，请使用 `simple` 或 `detailed`", finish=True)
        return

    logger.info(
        f"[{user_id}] 获取玩家 Rating 趋势, 查分器类型: {type(score_provider)}, 时间范围={range_desc}, 渲染模式={render_mode}"
    )
    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")

    user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)

    new_player_friend_code = None
    if user_bind_info is None and user_id.isdigit():
        logger.warning(f"[{user_id}] 未能获取玩家码，数据库中不存在绑定的玩家数据")
        logger.debug(f"[{user_id}] 1/4 尝试通过 QQ 请求玩家数据")
        try:
            player_info = await score_provider.fetch_player_info_by_qq(user_id)
            new_player_friend_code = player_info.friend_code
        except ClientResponseError as e:
            logger.warning(f"[{user_id}] 无法通过 QQ 号请求玩家数据: {e.code}: {e.message}")

            await _send_message(
                user_id, "查询 Rating 趋势的操作需要绑定落雪查分器喵，还请使用 /bind 指令进行绑定喵呜", finish=True
            )

            return

    friend_code = new_player_friend_code or user_bind_info.mai_friend_code  # type: ignore
    if not friend_code:
        logger.warning(f"[{user_id}] 无法获取好友码，无法继续查询。")
        await _send_message(user_id, "无法获取好友码，请确认已绑定或查分器可用。", finish=True)
        return

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家 Trend 趋势")

    trends = await score_provider.fetch_player_trend(friend_code)

    if range_days is not None:
        logger.debug(f"[{user_id}] 3/4 按时间窗口过滤 Trend 数据: {range_desc}")
        dated_trends: list[tuple[datetime, LXNSRatingTrend]] = []
        for trend in trends:
            date_text = str(trend.get("date", ""))
            try:
                trend_date = datetime.fromisoformat(date_text.replace("Z", "+00:00"))
            except ValueError:
                continue
            dated_trends.append((trend_date, trend))

        if not dated_trends:
            await _send_message(user_id, "趋势数据时间格式异常，暂时无法按时间范围筛选", finish=True)
            return

        latest_date = max(item[0] for item in dated_trends)
        start_date = latest_date - timedelta(days=range_days - 1)
        trends = [item[1] for item in dated_trends if item[0] >= start_date]

    if len(trends) < 5:
        await _send_message(
            user_id,
            (
                f"{range_desc}内的玩家 Rating 记录数据太少，无法进行渲染"
                if range_desc
                else "玩家 Rating 记录数据太少，无法进行渲染"
            ),
            finish=True,
        )
        return

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = draw_player_rating_trend(trends, show_standard_dx=(render_mode == "detailed"))
    byte = image_to_bytes(pic)

    await _send_message(user_id, "", images=[byte], finish=True)


@alconna_recommend.handle()
@catch_exception()
async def handle_recommend(
    db_session: async_scoped_session,
    event: Event,
    score_provider: MaimaiPyScoreProvider = Depends(get_maimaipy_provider),
):
    user_id = event.get_user_id()
    provider = await MaimaiPyScoreProvider.auto_get_score_provider(db_session, user_id)

    logger.info(f"[{user_id}] 获取玩家推分推荐, 查分器类型: {type(provider)}")
    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")

    identifier = await MaimaiPyScoreProvider.auto_get_player_identifier(db_session, user_id, provider)
    params = score_provider.ParamsType(provider, identifier)

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家所有成绩")
    if isinstance(provider, LXNSProvider):
        user_bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
        if user_bind_info is None or user_bind_info.lxns_api_key is None:
            await _send_message(
                event.get_user_id(),
                "你还没有绑定任何查分器喵，请先用 /bind 绑定一个查分器谢谢喵",
                finish=True,
            )
            return  # Avoid TypeError.

        identifier.credentials = user_bind_info.lxns_api_key
        params = score_provider.ParamsType(provider, identifier)
    player_scores = await score_provider.fetch_player_scoreslist(params)

    logger.debug(f"[{user_id}] 3/4 计算推分推荐...")
    st = perf_counter()
    player_scores.sort(key=lambda x: x.dx_rating, reverse=True)
    min_dx_score = player_scores[:50][-1].dx_rating
    recommend_songs = get_player_raise_score_songs(player_scores, round(min_dx_score))
    et = perf_counter()
    logger.debug(f"[{user_id}] 3/4 计算推分推荐用时 {(et - st):.3f}s")

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = DrawScores().draw_rise(recommend_songs, round(min_dx_score))
    byte = image_to_bytes(pic)

    await _send_message(user_id, "", images=[byte], finish=True)


@alconna_maistatus.handle()
@catch_exception(reply_prefix="获取舞萌状态失败")
async def handle_maistatus(event: Event):
    user_id = event.get_user_id()

    logger.debug("正在获取舞萌服务器状态...")
    try:
        status_text = await get_maistatus()
    except Exception as e:
        status_text = f"服务器状态检测失败：{e}"

    await _send_message(user_id, status_text)

    if not config.maistatus_url:
        return

    logger.debug("正在尝试获取舞萌状态截图...")
    try:
        st = perf_counter()
        png = await capture_maimai_status_png(config.maistatus_url)
        et = perf_counter()
        render_time_message = f"渲染用时 {et - st:.2f} 秒"
    except Exception as e:
        await _send_message(user_id, f"状态页截图渲染失败：{e}", finish=True)
        return

    await _send_message(user_id, render_time_message, images=[png], finish=True)


@alconna_netupdate.assign("$main")
@catch_exception(reply_prefix="查询机台更新事件失败")
async def handle_netupdate(
    event: Event,
    title_ver: Match[str] = AlconnaMatch("title_ver"),
):
    user_id = event.get_user_id()
    version = title_ver.result.strip() if title_ver.available else None

    logger.debug(f"[{user_id}] 正在查询机台更新事件（title_ver={version or '默认'}）")
    await _send_message(user_id, "正在查询机台更新事件，请稍候...")

    update_text = await get_update_event(title_ver=version)

    logger.debug(f"[{user_id}] 机台更新事件查询完成")
    await _send_message(user_id, update_text, finish=True)


@alconna_netupdate.assign("download")
@catch_exception(reply_prefix="下载机台更新包失败")
async def handle_netupdate_download(
    event: Event,
    title_ver: Match[str] = AlconnaMatch("title_ver"),
):
    user_id = event.get_user_id()

    if user_id not in get_driver().config.superusers:
        await _send_message(user_id, "下载机台更新包需要管理员权限哦", finish=True)

    version = title_ver.result.strip() if title_ver.available else None

    logger.info(f"[{user_id}] 开始下载机台更新包（title_ver={version or '默认'}）")
    await _send_message(user_id, "正在查询并下载机台更新包，耗时较长，请耐心等待...")

    download_text = await download_update_packages(title_ver=version)

    logger.info(f"[{user_id}] 机台更新包下载结束")
    await _send_message(user_id, download_text, finish=True)


@alconna_rikka.handle()
async def handle_rikka(db_session: async_scoped_session, event: Event):
    from .utils import get_version

    version = get_version()
    total_songs_count = len(await MaiSongORM.get_all_song_ids(db_session))

    message = (
        "Rikka 插件信息\n"
        f"插件版本: {version}\n"
        f"Bot 乐曲数量: {total_songs_count}\n"
        f"机台源支持: {'已启用' if config.enable_arcade_provider else '未启用'}\n"
        f"机台写入支持: {'已启用' if config.enable_arcade_write else '未启用'}\n"
        f"标签数据: {'已启用' if SONG_TAGS_DATA_AVAILABLE else '未启用'}\n"
        f"状态页支持: {'已启用' if config.maistatus_url else '未启用'}\n"
    )

    await _send_message(event.get_user_id(), message)

    await handle_help(event)


# --- 中二节奏 (CHUNITHM) handlers ---
@event_preprocessor
async def _normalize_chu_leading_at(event: Event) -> None:
    """把「@某人 /chu ...」前面的 @ 去掉，让命令能被 Alconna 识别。

    被 @ 的用户信息仍保留在 original_message 中，由 _get_at_target 读取。
    """
    # 只有消息事件才有 message 字段；心跳/通知等事件调用 get_message() 会抛
    # NotImplementedError，不能作为判断依据。
    message = getattr(event, "message", None)
    if message is None:
        return
    if len(message) < 2 or message[0].type != "at":
        return
    index = 0
    while index < len(message) and message[index].type == "at":
        index += 1
    if index >= len(message) or message[index].type != "text":
        return
    if not str(message[index]).lstrip().startswith("/chu"):
        return
    event.message = type(message)(list(message[index:]))


def _get_at_target(event: Event) -> str | None:
    """取消息里第一个 @ 的用户 QQ；忽略 @全体成员 和 @机器人自己。"""
    try:
        self_id = current_bot.get().self_id
    except Exception:
        self_id = getattr(event, "self_id", None)
    # 前置 @ 会被 _normalize_chu_leading_at 从 message 里移除，需回看原始消息
    message = getattr(event, "original_message", None)
    if message is None:
        message = event.get_message()
    for seg in message:
        if seg.type != "at":
            continue
        qq = str(seg.data.get("qq", ""))
        if qq and qq != "all" and qq != str(self_id):
            return qq
    return None


async def _get_chu_params(
    db_session: async_scoped_session, user_id: str, lxns_bind_required: bool = False
) -> LXNSChuParams:
    """
    根据用户绑定信息自动获取中二节奏查分参数

    一般看作只要存在 friend_code 就算绑定了落雪查分器,
    但存在一个显著的问题，开发者 API 无法获取完整的 scorelist,
    所以当部分需要使用个人 API 查询的功能时需要要求绑定落雪查分器

    :param lxns_bind_required: 使用个人 API 时强制要求绑定落雪查分器
    """
    bind_info = await UserBindInfoORM.get_user_bind_info(db_session, user_id)
    # Rikka 自己的表里没密钥时，回退用底力分析插件共享的个人密钥。
    user_key = (bind_info.lxns_api_key if bind_info else "") or get_shared_api_key(user_id) or ""

    # 优先复用「中二节奏底力分析」插件里已完成的绑定：用户在那边发过
    # /bind lxns <好友码> 之后，这里不必再绑一次（见 shared_binding.py）。
    shared_code = get_shared_friend_code(user_id)
    if shared_code:
        if lxns_bind_required and not user_key:
            # 个人 API 类功能仍需要个人密钥，但不再要求重新绑定好友码。
            logger.debug(f"[{user_id}] 命中底力分析共享绑定，但缺少个人 API 密钥")
            await _send_message(
                user_id,
                "你已在底力分析中绑定落雪，但该功能需要落雪个人 API 密钥。"
                "请使用 /bind key <密钥> 补充绑定喵~",
                finish=True,
            )
        logger.debug(f"[{user_id}] 复用底力分析插件的共享绑定: {shared_code}")
        return LXNSChuParams(
            friend_code=int(shared_code),
            qq=user_id,
            user_key=user_key or None,
        )

    if lxns_bind_required:
        if not user_key:
            await _send_message(
                user_id,
                "成绩的查询需要落雪个人 API 密钥哦, 请使用 /bind key <密钥> 绑定喵~",
                finish=True,
            )

    if (not bind_info) or (not bind_info.chu_friend_code):
        if user_id.isdigit():
            logger.warning("未能获取到玩家绑定信息，尝试使用 QQ 号查询...")
            try:
                provider = get_lxns_chu_provider()
                info = await provider.fetch_player_info_by_qq(user_id)
                return LXNSChuParams(friend_code=info.friend_code)
            except Exception as e:
                logger.warning(f"[{user_id}] 中二好友码获取失败: {e}")

        await _send_message(
            user_id,
            "成绩的查询需要绑定落雪查分器哦, 请使用 /bind lxns <落雪好友码> 绑定喵~",
            finish=True,
        )

    assert bind_info and bind_info.chu_friend_code
    return LXNSChuParams(
        friend_code=int(bind_info.chu_friend_code), qq=user_id, user_key=user_key or None
    )


@alconna_chu.assign("b50")
@catch_exception("获取中二节奏 Best 50 失败")
async def handle_chu_b50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
):
    user_id = _get_at_target(event) or event.get_user_id()

    logger.info(f"[{user_id}] [中二节奏] 获取玩家 Best 50")
    logger.debug(f"[{user_id}] 1/3 获得用户鉴权凭证...")
    params = await _get_chu_params(db_session, user_id)

    logger.debug(f"[{user_id}] 2/3 发起 API 请求玩家 Best 50...")
    player_info = await score_provider.fetch_player_info(params)
    bests = await score_provider.fetch_player_bests(params)

    logger.debug(f"[{user_id}] 3/3 渲染玩家数据...")
    pic = await chu_renderer.render_chu_bests(player_info, bests)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_chu.assign("r50")
@catch_exception("获取中二节奏 Recent 50 失败")
async def handle_chu_r50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
):
    user_id = _get_at_target(event) or event.get_user_id()

    logger.info(f"[{user_id}] [中二节奏] 获取玩家 Recent 50")
    params = await _get_chu_params(db_session, user_id)

    player_info = await score_provider.fetch_player_info(params)
    recents = await score_provider.fetch_player_recents(params)

    if not recents:
        await _send_message(user_id, "暂无最近游玩记录", finish=True)
        return

    logger.debug(f"[{user_id}] 渲染玩家数据...")
    pic = await chu_renderer.render_chu_player_scores(recents, player_info, title="Recent 50 列表")

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_chu.assign("plate")
@catch_exception("设置名片底板失败")
async def handle_chu_plate(
    event: Event,
    plate_id: Match[int] = AlconnaMatch("plate_id"),
):
    """设置成绩图使用的名牌底板（收藏品个性化）。

    落雪开发者接口不返回玩家装备的 name_plate，所以由用户自己指定，
    结果存到 data/chu_name_plates.json，重启后依然有效。
    """
    user_id = event.get_user_id()
    if not plate_id.available:
        await _send_message(
            user_id,
            "用法：/chu plate <ID>\n"
            "可选 ID 见 static/chu/plate/，例如 /chu plate 8；"
            "/chu plate 0 使用纯色底板。",
            finish=True,
        )
        return
    pid = int(plate_id.result)
    plate_path = Path(config.static_resource_path) / "chu" / "plate" / f"{pid}.png"
    if pid != 0 and not plate_path.exists():
        available = sorted(
            int(p.stem) for p in plate_path.parent.glob("*.png") if p.stem.isdigit()
        )
        await _send_message(
            user_id,
            f"没有找到 ID 为 {pid} 的名牌底板。\n可用 ID：{available}",
            finish=True,
        )
        return

    plate_file = Path("data") / "chu_name_plates.json"
    try:
        saved: dict[str, int] = (
            json.loads(plate_file.read_text(encoding="utf-8")) if plate_file.exists() else {}
        )
    except Exception:
        saved = {}
    saved[str(user_id)] = pid
    plate_file.parent.mkdir(parents=True, exist_ok=True)
    plate_file.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同步到进程内配置，立即生效
    config.chu_name_plate_by_user = {**config.chu_name_plate_by_user, str(user_id): pid}

    msg = f"已把你的成绩图名片底板设为 {pid}。"
    if pid == 0:
        msg += "（纯色底板）"
    await _send_message(user_id, msg, finish=True)


@alconna_chu.assign("ap50")
@catch_exception("获取中二节奏 AP 50 失败")
async def handle_chu_ap50(
    event: Event,
    db_session: async_scoped_session,
    score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
):
    user_id = _get_at_target(event) or event.get_user_id()

    logger.info(f"[{user_id}] [中二节奏] 获取玩家 AP 50")
    params = await _get_chu_params(db_session, user_id)

    player_info = await score_provider.fetch_player_info(params)

    from .score.chunithm._schema import ChuFullComboType

    # ── 阶段一：从「玩家全部成绩」里筛出 full_combo 为 AJ / AJC 的曲目 ──
    # /scores 返回玩家所有游玩过的谱面（含低难度），是 AJ 唯一可靠的来源。
    # 注意它只有评级、没有分数，分数要靠阶段二补。
    try:
        all_scores = await score_provider.fetch_player_scores(
            params, use_user_api=bool(params.user_key)
        )
    except Exception as e:
        logger.warning(f"[{user_id}] 全成绩接口不可用: {e}")
        all_scores = []

    ap_scores: list[PlayerChuScore] = [
        s for s in all_scores
        if s.full_combo in (ChuFullComboType.AJ, ChuFullComboType.AJC)
    ]

    # 兜底：全成绩接口拿不到时（如未绑定且接口异常），退回用 /bests 的 60 首筛
    if not ap_scores:
        logger.info(f"[{user_id}] 全成绩无结果，回退 /bests 筛选")
        bests = await score_provider.fetch_player_bests(params)
        best_scores = bests.bests + bests.selections + bests.new_bests
        ap_scores = [
            s for s in best_scores
            if s.full_combo in (ChuFullComboType.AJ, ChuFullComboType.AJC)
        ]
    else:
        best_scores = []

    # ── 阶段二：用「获取 B50 的方式」补齐这些曲目的具体成绩 ──
    # /bests 与 /recents 返回的条目带 score / rating / over_power，
    # 按 (曲目, 难度) 建索引，命中就用带分数的版本覆盖到 AJ 曲目上。
    # 已绑定个人密钥时 /scores 本身就带分数，无需补分。
    if ap_scores and any(s.score == 0 for s in ap_scores):
        score_lookup: dict[tuple[int, int], PlayerChuScore] = {}
        try:
            if not best_scores:
                bests = await score_provider.fetch_player_bests(params)
                best_scores = bests.bests + bests.selections + bests.new_bests
            for s in best_scores:
                score_lookup[(s.song_id, int(s.song_difficulty.value))] = s
        except Exception as e:
            logger.warning(f"[{user_id}] 获取 B50 补分失败: {e}")

        try:
            recents = await score_provider.fetch_player_recents(params)
            for s in recents:
                if s.score > 0:
                    score_lookup[(s.song_id, int(s.song_difficulty.value))] = s
        except Exception as e:
            logger.warning(f"[{user_id}] 获取 Recent 50 补分失败: {e}")

        for s in ap_scores:
            full = score_lookup.get((s.song_id, int(s.song_difficulty.value)))
            if full is not None and full.score > 0:
                # 保留原成绩的 AJ/AJC 标记，只覆盖数值与曲目信息
                s.score = full.score
                s.rating = full.rating
                s.over_power = full.over_power
                s.song_name = full.song_name
                s.song_level = full.song_level

    ap_scores.sort(key=lambda x: (x.rating, x.score), reverse=True)
    logger.info(f"[{user_id}] 筛出 {len(ap_scores)} 首 AJ 成绩")

    if not ap_scores:
        await _send_message(user_id, "暂无 ALL JUSTICE 成绩", finish=True)
        return

    # 正常生成图片：即使部分/全部曲目缺分数也照常出图，不提前拦截。
    logger.debug(f"[{user_id}] 渲染玩家数据...")
    pic = await chu_renderer.render_chu_player_scores(ap_scores, player_info, title="AP 50 列表")

    # 图片之后追加提示：拿不到具体分数时，告诉用户怎么补绑。
    no_score = sum(1 for s in ap_scores if not s.score)
    if no_score:
        # 第一段只发图（不 finish），第二段发提示后结束
        await _send_message(user_id, "", images=[pic])
        await _send_message(
            user_id,
            f"其中 {no_score} 首曲目未显示具体分数（落雪好友码接口不返回低难度谱面分数）。\n"
            "如需显示分数，请发送 /bind key <落雪个人 API 密钥> 补充绑定。",
            finish=True,
        )
    else:
        await _send_message(user_id, "", images=[pic], finish=True)


@alconna_chu.assign("minfo")
@catch_exception("获取中二节奏乐曲信息失败")
async def handle_chu_minfo(
    event: Event,
    db_session: async_scoped_session,
    name: Match[UniMessage] = AlconnaMatch("name"),
):
    user_id = event.get_user_id()

    if not name.available:
        await _send_message(user_id, "请输入有效的乐曲ID/名称/别名！", finish=True)

    raw_query = name.result.extract_plain_text().strip()
    logger.info(f"[{user_id}] [中二节奏] 查询乐曲信息, 查询内容: {raw_query}")

    try:
        song = await get_chusong_by_id_or_alias(db_session, raw_query)
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    message, images = _build_chusong_info_message(song)
    await _send_message(user_id, message, images, finish=True)


@alconna_chu.assign("random")
@catch_exception("获取中二节奏随机乐曲失败")
async def handle_chu_random(
    event: Event,
    db_session: async_scoped_session,
):
    user_id = event.get_user_id()

    logger.info(f"[{user_id}] [中二节奏] 随机抽取乐曲")

    song_ids = await ChuSongORM.get_all_song_ids(db_session)
    if not song_ids:
        await _send_message(user_id, "乐曲数据库为空，请先执行 .update songs", finish=True)
        return

    songs = await ChuSongORM.get_songs_info_by_ids(db_session, list(song_ids))
    if not songs:
        await _send_message(user_id, "获取乐曲列表失败", finish=True)
        return

    song = random.choice(songs)
    await chu_renderer._ensure_cover(song.id)
    message, images = _build_chusong_info_message(song)
    await _send_message(user_id, message, images, finish=True)


@alconna_chu.assign("score")
@catch_exception()
async def handle_chu_score(
    event: Event,
    db_session: async_scoped_session,
    name: Match[UniMessage] = AlconnaMatch("name"),
    score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
):
    user_id = _get_at_target(event) or event.get_user_id()

    if not name.available:
        await _send_message(user_id, "请输入有效的乐曲ID/名称/别名！", finish=True)

    raw_query = name.result.extract_plain_text().strip()
    logger.info(f"[{user_id}] 查询单曲游玩情况, 查询内容: {raw_query}")

    logger.debug(f"[{user_id}] 1/4 通过乐曲ID/别名查询乐曲信息...")
    try:
        song = await get_chusong_by_id_or_alias(db_session, raw_query)
    except ValueError as e:
        await _send_message(user_id, str(e), finish=True)
        return

    logger.debug(f"[{user_id}] 2/4 获得用户鉴权凭证...")
    params = await _get_chu_params(db_session, user_id, lxns_bind_required=True)
    logger.debug(f"[{user_id}] 1/4 鉴权参数: {params}")

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家信息...")
    scores = await score_provider.fetch_player_scores(params)
    scores = [s for s in scores if s.song_id == song.id]

    if not scores:
        await _send_message(user_id, f"未找到乐曲 '{song.title}' 的游玩记录喵~不如先去挑战一下吧~", finish=True)
        return

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await chu_renderer.render_chu_player_song_info(song, scores)

    await _send_message(user_id, "", images=[pic], finish=True)


@alconna_chu.assign("scorelist")
@catch_exception()
async def handle_chu_scorelist(
    event: Event,
    db_session: async_scoped_session,
    arg: Match[UniMessage] = AlconnaMatch("arg"),
    score_provider: LXNSChuScoreProvider = Depends(get_lxns_chu_provider),
):
    user_id = _get_at_target(event) or event.get_user_id()

    if not arg.available or not arg.result:
        await _send_message(
            user_id,
            "/scorelist 使用帮助\n"
            "/scorelist <level> 获取指定等级的成绩列表\n"
            "/scorelist <level_value> 获取指定等级（定数）的成绩列表\n"
            "/scorelist ach<float> 获取指定分数的成绩列表\n"
            "/scorelist <diff> 获取指定铺面难度的成绩列表\n"
            "/scorelist <条件> [page] 获取指定页（每页 50 条）\n"
            "eg.\n"
            "/scorelist 12+\n"
            "/scorelist 12.7\n"
            "/scorelist ach1008000\n"
            "/scorelist ach1008"
            "/scorelist expert\n"
            "/scorelist 12+ 2",
            finish=True,
        )

    raw_input = arg.result.extract_plain_text().strip()
    query_parts = raw_input.split()

    if not query_parts:
        await _send_message(user_id, "命令格式错误，请检查后重新输入！", finish=True)
        return

    raw_query = query_parts[0]
    page = 1
    if len(query_parts) >= 2:
        if not query_parts[1].isdigit() or int(query_parts[1]) <= 0:
            await _send_message(user_id, "页码必须是大于 0 的整数", finish=True)
            return
        page = int(query_parts[1])

    if len(query_parts) > 2:
        await _send_message(user_id, "命令格式错误，请检查后重新输入！", finish=True)
        return

    logger.info(f"[{user_id}] 查询指定条件的成绩列表, 查询内容: {raw_query}")
    level: Optional[str] = None
    level_value: Optional[float] = None
    ach: Optional[int] = None
    diff: Optional[str] = None
    if raw_query.isdigit() or (raw_query.endswith("+") and raw_query[:-1].isdigit()):
        level = raw_query
        title = f"等级 {level} 成绩列表"
    elif is_float(raw_query):
        level_value = float(raw_query)
        title = f"定数 {level_value} 成绩列表"
    elif raw_query.startswith("ach") and raw_query[3:].isnumeric():
        ach_s = raw_query[3:]
        while len(ach_s) < 4:
            ach_s += "0"
        ach_s = ach_s[:4]
        ach = int(ach_s) * 1000
        title = f"达成率 {ach} 成绩列表"
    elif raw_query.upper() in ["BASIC", "ADVANCED", "EXPERT", "MASTER", "ULTIMA", "WORLDS_END", "WORLDSEND"]:
        diff = raw_query.upper()
        diff = "WORLDS_END" if diff == "WORLDSEND" else diff
        title = f"铺面等级 {diff} 成绩列表"
    else:
        await _send_message(user_id, "命令格式错误，请检查后重新输入！", finish=True)
        return

    logger.debug(f"[{user_id}] 1/4 获得用户鉴权凭证...")
    params = await _get_chu_params(db_session, user_id, lxns_bind_required=True)
    logger.debug(f"[{user_id}] 1/4 鉴权参数: {params}")

    logger.debug(f"[{user_id}] 2/4 发起 API 请求玩家信息...")
    player_info = await score_provider.fetch_player_info(params)

    logger.debug(f"[{user_id}] 3/4 发起 API 请求玩家全部成绩...")
    scores = await score_provider.fetch_player_scores(params, use_user_api=True)

    filtered_scores: list[PlayerChuScore] = []

    for score in scores:
        if (
            (level and score.song_level == level)
            or (ach and score.score // 1000 == ach)
            or (diff and score.song_difficulty.name == diff)
        ):
            filtered_scores.append(score)
        if level_value:
            song_id = score.song_id
            song = ChuSongORM.get_song_sync(song_id)
            assert song, f"数据库中不存在乐曲 {song_id} 的数据，请联系管理员处理"
            song_level_value = song.difficulties.difficulties[score.song_difficulty.value].level_value
            if round(level_value, 1) == round(song_level_value, 1):
                filtered_scores.append(score)

    if not filtered_scores:
        await _send_message(user_id, "呜呜，未找到符合条件的游玩记录喵", finish=True)
        return

    # 严格按实际打出的分数从高到低排序（同分时按 rating 降序）
    filtered_scores.sort(key=lambda s: (s.score, s.rating), reverse=True)

    page_size = 50
    total_pages = (len(filtered_scores) + page_size - 1) // page_size
    start = (page - 1) * page_size
    end = start + page_size
    page_scores = filtered_scores[start:end]

    if not page_scores:
        await _send_message(
            user_id, f"页码超出范围，当前共 {total_pages} 页，请输入 1~{total_pages} 之间的页码", finish=True
        )
        return

    title = f"{title} - 第 {page}/{total_pages} 页"

    logger.debug(f"[{user_id}] 4/4 渲染玩家数据...")
    pic = await chu_renderer.render_chu_player_scores(page_scores, player_info, title)

    await _send_message(user_id, "", images=[pic], finish=True)

