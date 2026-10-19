"""中二节奏底力分析 —— 插件包。

目录结构：

    src/plugins/chunithm_bot/
    ├── __init__.py   插件入口：声明元信息，加载子模块
    ├── lxns.py       落雪 API 客户端：URL、鉴权、错误翻译、曲目缓存（只管「怎么问落雪」）
    ├── analysis.py   数据层：绑定库、统计、AI 简报、报告图片渲染（只管「数据怎么算」）
    └── commands.py   指令层：/中二绑定系列、/底力分析（只管「跟用户怎么聊」）

依赖方向是单向的：commands → analysis → lxns，没有反向依赖。
要给插件加新的落雪能力（例如查趋势、查单曲排名），优先在 lxns.py 里加一个
``fetch_xxx`` 函数，再由 analysis 组合。

/help 与 /hello 是独立插件（src/plugins/help.py、src/plugins/hello.py），不放在本包内。

NoneBot 只要求插件包位于 plugin_dirs（pyproject.toml 的 src/plugins）下即可被扫描到；
子模块必须在 __init__.py 里显式导入，否则其中的 on_command 不会注册。
"""

from nonebot.plugin import PluginMetadata

# 数据层对外接口，便于其它脚本/测试直接调用，无需关心内部模块划分。
# 需要直接访问落雪接口时也可以 `from .lxns import ...`（tests/ 里就是这么做的）。
from .analysis import (  # noqa: F401
    AnalysisError,
    compute_report,
    fetch_report,
    fetch_song_list,
    get_binding,
    init_database,
    local_summary,
    render_report,
)

__plugin_meta__ = PluginMetadata(
    name="中二节奏底力分析",
    description="查询落雪（LXNS）中二节奏成绩，生成底力分析图片报告。",
    usage=(
        "/中二绑定 好友码 完成绑定后，发送 /底力分析 生成报告。"
        "群聊、私聊都可用；发送 /help 查看全部指令。"
    ),
    type="application",
    homepage="",
    supported_adapters={"~onebot.v11"},
)

# 导入子模块，导入即注册对应的指令 matcher。
from . import commands  # noqa: E402,F401
