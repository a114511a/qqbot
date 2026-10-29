from pathlib import Path

from nonebot import logger, require

require("nonebot_plugin_alconna")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")
require("nonebot_plugin_session")
require("nonebot_plugin_apscheduler")

from nonebot.plugin import PluginMetadata, inherit_supported_adapters  # noqa: E402

from .config import Config, config  # noqa: E402
from .utils import init_logger  # noqa: E402

init_logger()

from nonebot import get_driver  # noqa: E402
from nonebot_plugin_orm import get_scoped_session  # noqa: E402

from . import alconna  # noqa: E402, F401
from . import database  # noqa: E402, F401
from .database import ChuSongORM, MaiSongORM  # noqa: E402


@get_driver().on_startup
async def check_static_resource():
    """检查静态资源是否就绪。

    上游实现是「缺资源直接抛异常拒绝启动」，但本项目的静态资源包体积很大、
    官方下载链接已失效，为了不让整个机器人因为缺背景图而起不来，
    这里降级为**告警**：缺什么就记一条日志，对应功能（成绩图渲染）会回滚到
    纯色背景/自动下载单张资源，其它指令不受影响。

    想恢复严格模式：把下面的 logger.warning 改回 raise 即可。
    """
    from .painters.maimai._config import FONT_DIR, PLATE_DIR

    static_dir = Path(config.static_resource_path)
    if not static_dir.exists():
        logger.warning(
            f"[静态资源] 目录不存在: {static_dir}，成绩图会使用纯色背景。"
            "完整资源包见项目 README 的静态资源说明。"
        )
        return

    if not PLATE_DIR.exists():
        logger.warning(
            f"[静态资源] 未找到舞萌名牌资源 {PLATE_DIR}，成绩图的名牌区域会留空。"
        )

    if not (FONT_DIR / "ResourceHanRoundedCN-Bold.ttf").exists() and not config.scorelist_font_main:
        logger.warning(
            "[静态资源] 未找到 ResourceHanRoundedCN-Bold.ttf，成绩图将回退到系统字体。"
        )


@get_driver().on_startup
async def initialize_song_cache():
    session = get_scoped_session()
    logger.debug("更新乐曲缓存中...")
    await MaiSongORM.refresh_cache(session)
    await ChuSongORM.refresh_cache(session)


@get_driver().on_startup
async def migrate_chu_friend_codes():
    """数据迁移：为已有舞萌绑定但缺少中二好友码的用户自动补全 chu_friend_code。"""
    from sqlalchemy import select

    from .database import UserBindInfoORM
    from .database.orm_models import UserBindInfo
    from .score.chunithm import get_lxns_chu_provider

    session = get_scoped_session()
    provider = get_lxns_chu_provider()

    result = await session.execute(
        select(UserBindInfo).where(
            (UserBindInfo.lxns_api_key.isnot(None) | UserBindInfo.lxns_api_key != ""),
            (UserBindInfo.chu_friend_code.is_(None) | (UserBindInfo.chu_friend_code == "")),
        )
    )
    users = result.scalars().all()

    if not users:
        return

    logger.info(f"[数据迁移] 发现 {len(users)} 个用户需要补全中二好友码")

    migrated = 0
    for user in users:
        uid = user.user_id  # 提前提取，避免 commit 后 ORM 属性过期
        try:
            assert user.lxns_api_key
            info = await provider.fetch_player_info_by_user_token(user.lxns_api_key)
            await UserBindInfoORM.set_user_chu_friend_code(session, uid, str(info.friend_code))
            migrated += 1
            logger.debug(f"[数据迁移] 用户 {uid} 中二好友码已补全: {info.friend_code}")
        except Exception as e:
            logger.debug(f"[数据迁移] 用户 {uid} 中二好友码补全失败: {e}")

    if migrated:
        logger.info(f"[数据迁移] 成功补全 {migrated} 个用户的中二好友码")


__plugin_meta__ = PluginMetadata(
    name="Nonebot-Plugin-Rikka",
    description="一个简单的舞萌成绩查询Bot插件，同时支持落雪咖啡屋和水鱼查分器",
    usage=".rikka",
    type="application",
    config=Config,
    homepage="https://github.com/Moemu/Nonebot-Plugin-Rikka",
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
)
