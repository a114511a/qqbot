from pathlib import Path
from typing import Optional

from nonebot import get_plugin_config
from pydantic import BaseModel, Field, validator


class Config(BaseModel):
    log_level: str = "debug"
    """日志等级"""
    add_alias_need_admin: bool = True
    """添加别名需要管理员权限"""
    static_resource_path: str = str(Path() / "static")
    """静态资源路径"""

    lxns_developer_api_key: Optional[str] = None
    """落雪咖啡屋开发者密钥"""
    divingfish_developer_api_key: Optional[str] = None
    """水鱼查分器开发者密钥"""
    divingfish_client_id: Optional[str] = None
    """水鱼查分器 Client ID"""
    divingfish_client_secret: Optional[str] = None
    """水鱼查分器 Client Secret"""

    enable_arcade_provider: bool = False
    """启用 Maimai.py 的机台源查询"""
    enable_arcade_write: bool = False
    """启用机台写入操作（六倍票、强制登出、解锁铺面等）"""
    arcade_provider_http_proxy: Optional[str] = None
    """机台源的代理地址（当前不可用）"""

    maistatus_url: Optional[str] = "https://status.snowy.moe/status/maimai"
    """舞萌状态页地址，用于渲染 .maistatus """

    scorelist_bg: Optional[str] = None
    """成绩图背景，建议比例 8:7"""
    scorelist_font_main: Optional[str] = None
    """成绩图主字体文件"""
    scorelist_font_color: tuple[int, int, int, int] = (124, 129, 255, 255)
    """成绩图默认文字颜色"""
    scorelist_font_num: Optional[str] = None
    """成绩图数字字体文件"""
    scorelist_element_opacity: float = Field(1.0, le=1.0, ge=0.0)
    """成绩图元素不透明度"""

    # 名片个性化：落雪开发者接口**只在玩家公开名牌时**才返回 name_plate，
    # 不公开的玩家走自动兜底（挑一张有真实画面的底板，不会用 CDN 上那张
    # 纯白的 plate/1）。这里允许按 QQ 号或全局指定底板 ID。
    chu_name_plate_id: Optional[int] = None
    """中二成绩图默认名牌底板 ID（对所有玩家生效）"""
    chu_default_name_plate: Optional[int] = None
    """兜底底板 ID；不填则自动挑一张有内容的底板。填 0 表示改用纯色底板"""
    chu_name_plate_by_user: dict[str, int] = {}
    """按 QQ 号指定的名牌底板 ID，优先于默认，例如 {"484413313": 8}"""

    enable_subscribe_function: bool = False
    """启用机厅列表更新订阅功能（需要平台支持，建议在测试后使用）"""

    @validator("static_resource_path")
    def validate_static_resource_path(cls, v: str) -> str:
        p = Path(v)
        if not p.exists() or not p.is_dir():
            raise ValueError(f"资源文件夹: {v} 不存在！请下载静态资源文件或者重新配置静态资源文件路径")
        return str(p.resolve())


config = get_plugin_config(Config)
