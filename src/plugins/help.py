"""帮助指令：/help 提供分层索引，具体功能按分类查看。

设计目标（按需求）：
- 直接发 ``/help`` 只回三大类目录，不再一屏刷出全部指令；
- ``/help mai`` 看舞萌 DX 指令，``/help chu`` 看中二节奏指令，``/help bind`` 看账号绑定；
- 群里直接发指令即可，不需要 @机器人（本插件不注册 to_me 规则）。

帮助文本集中在下面几个常量里，改指令时同步这一处即可。
本插件刻意不依赖其它插件包，保证即使某个插件加载失败也能查到索引。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg

# ── 总索引：只给三类入口，不列具体指令 ────────────────────────────────────
HELP_INDEX = """机器人指令索引
—————————————
本机器人有三类功能，发送对应指令查看详细用法：

【舞萌 DX】
  /help mai   查看舞萌全部指令
  常用：/mai b50、/mai r50、/mai ap50

【中二节奏 CHUNITHM】
  /help chu   查看中二全部指令
  常用：/底力分析、/chu b30、/chu r50

【账号绑定】
  /help bind  查看绑定、解绑、切换数据源
  已绑定过落雪的话，两个插件共享同一份绑定，无需重复绑定

—————————————
群里直接发指令即可，不需要 @机器人。"""

# ── 舞萌 DX 子索引 ────────────────────────────────────────────────────────
HELP_MAI = """舞萌 DX · 指令列表
—————————————
【成绩查询】
/mai b50              玩家 Best 50
/mai ap50             玩家 ALL PERFECT 50
/mai r50              玩家 Recent 50
/mai n50              玩家拟合系数 Top-50
/mai pc50             玩家游玩次数 Top-50
/mai score <曲名|ID>  单曲游玩情况
/mai scorelist <条件> [页码]  条件成绩列表

【曲目相关】
/mai minfo <曲名|ID>  乐曲信息
/mai random [难度] [等级]  随机一首乐曲
/alias                管理乐曲别名

【其它】
/trend [时间范围]     Rating 趋势
/今日舞萌             今日出勤运势
/舞萌状态             服务器状态
/update songs|alias|chart  更新本地数据库

—————————————
发送 /help 返回总索引。"""

# ── 中二节奏子索引 ────────────────────────────────────────────────────────
HELP_CHU = """中二节奏 CHUNITHM · 指令列表
—————————————
【成绩分析】
/底力分析             生成底力分析图片报告
                      别名：/中二底力分析、/chunithm底力分析
/chu b30              玩家 Best 30
/chu r50              玩家 Recent 50
/chu ap30             玩家 ALL PERFECT 30
/chu score <曲名|ID>  单曲游玩情况
/chu scorelist [等级] [页码]  成绩列表

【曲目相关】
/chu minfo <曲名|ID>  乐曲信息
/chu random           随机一首乐曲

【账号绑定】
/中二绑定 好友码      绑定落雪玩家（15 位数字）
/中二绑定确认         确认绑定（10 分钟内有效）
/中二绑定状态         查看当前绑定
/中二解绑             解除绑定

—————————————
绑定一次即可：底力分析与查分插件共享同一份绑定。
发送 /help 返回总索引。"""

# ── 账号绑定子索引 ────────────────────────────────────────────────────────
HELP_BIND = """账号绑定 · 指令列表
—————————————
【落雪咖啡屋】
/中二绑定 好友码      绑定落雪玩家（推荐，两个插件共用）
/bind lxns <个人API密钥>   绑定落雪个人 API（部分功能需要）
/unbind lxns          解绑落雪

【水鱼查分器】
/bind divingfish      绑定水鱼查分器
/unbind divingfish    解绑水鱼

【通用】
/bind                 查看当前绑定情况
/unbind all           解除全部绑定
/source lxns|divingfish    设置默认数据源

—————————————
说明：在中二节奏底力分析里绑定过落雪后，查分插件会自动复用，
不需要再绑一次。发送 /help 返回总索引。"""

SUBCOMMANDS: dict[str, str] = {
    "mai": HELP_MAI,
    "chu": HELP_CHU,
    "bind": HELP_BIND,
}

help_command = on_command("help", aliases={"帮助", "指令", "菜单"}, priority=5, block=True)


@help_command.handle()
async def handle_help(argument: Message = CommandArg()) -> None:
    # /help mai、/help chu、/help bind 取细分索引；裸 /help 给总索引。
    sub = argument.extract_plain_text().strip().lower()
    text = SUBCOMMANDS.get(sub, HELP_INDEX)
    # 帮助不含个人信息，群聊私聊都直接返回。
    await help_command.finish(Message(text))
