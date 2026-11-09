"""机厅人数记录插件。

自包含重写，替代旧项目的 ``nonebot-plugin-mai-arcade 0.1.5.7.5``（785 行单文件）。
保留玩家已熟悉的触发格式，去掉排卡/地图/别名等冗余功能，并修掉原版几个问题：

- 原版 ``sv_arcade = on_regex(r'^(?!.*[+-]\\d+)(.*?)\\d+$|^(.*?)[+-=]+$')`` 过宽，
  任何以数字结尾的消息都会被触发；这里改成「先拿机厅名，命中已知机厅才处理」。
- 原版 ``<机厅名>--`` 是 ``num.pop()``（弹掉最后一条记录，不等于 -1）；
  这里改成真正的 -1，语义符合直觉。
- 原版每日 0 点清零只改内存不落盘，重启后人数复活；这里清零后立即写文件。
- 原版十几个 handler 误用 ``go_on.finish()``；这里各用各的 matcher。

命令：
    <机厅名>++ / <机厅名>--            人数 +1 / -1
    <机厅名>+N / <机厅名>-N            人数 +N / -N
    <机厅名>=N / <机厅名>N             人数重置为 N
    <机厅名>几 / 几人 / j              查询单个机厅
    /机厅人数                           查询今日有更新的所有机厅
    /添加机厅别名 <机厅名><别名>        为机厅添加别名（管理员）

数据存 ``static/arcade_data.json``，按群隔离。
每日 0 点自动清空人数并写回文件。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from nonebot import get_plugin_config, logger, on_command, on_message, require
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from pydantic import BaseModel

require("nonebot_plugin_apscheduler")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_FILE = PROJECT_ROOT / "static" / "arcade_data.json"


class Config(BaseModel):
    # 默认所有群生效；只在需要屏蔽个别群时才填 disabled_groups。
    disabled_groups: list[str] = []


config = get_plugin_config(Config)

__plugin_meta__ = PluginMetadata(
    name="机厅人数",
    description="记录并查询各机厅当前人数",
    usage=(
        "<机厅名>++ / -- 人数 ±1；<机厅名>+N / -N 增减 N；"
        "<机厅名>=N 重置为 N；<机厅名>几 查询；/机厅人数 查看全部；"
        "/添加机厅别名 <机厅名><别名> 为机厅添加别名"
    ),
    type="application",
    supported_adapters={"~onebot.v11"},
)

# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

_data: dict[str, dict] = {}
_loaded = False


def _migrate_legacy(data: dict[str, dict]) -> dict[str, dict]:
    """兼容旧版插件的字段名（num / last_updated_by / last_updated_at）。"""
    for group in data.values():
        if not isinstance(group, dict):
            continue
        for entry in group.values():
            if not isinstance(entry, dict):
                continue
            if "deltas" not in entry:
                nums = entry.get("num")
                nums = nums if isinstance(nums, list) else []
                entry["deltas"] = [int(x) for x in nums if isinstance(x, (int, float))]
            entry.setdefault("by", entry.get("last_updated_by") or "")
            entry.setdefault("at", entry.get("last_updated_at") or "")
            for key in ("num", "last_updated_by", "last_updated_at"):
                entry.pop(key, None)
    return data


def _load() -> dict[str, dict]:
    global _data, _loaded
    if _loaded:
        return _data
    if DATA_FILE.exists():
        try:
            _data = _migrate_legacy(json.loads(DATA_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning(f"[机厅] 读取数据失败，使用空数据: {e}")
            _data = {}
    _loaded = True
    return _data


def _save() -> None:
    try:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_FILE.write_text(
            json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.error(f"[机厅] 写入数据失败: {e}")


def _group_data(group_id: int) -> dict:
    data = _load()
    return data.setdefault(str(group_id), {})


def _name_map(group_id: int) -> dict[str, str]:
    """群内可用的触发名 → 机厅正式名：机厅名本身 + 各自别名。"""
    gd = _group_data(group_id)
    names = {name: name for name in gd}
    for name, entry in gd.items():
        aliases = entry.get("alias_list")
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            # 纯数字别名会和「<机厅名>N」的人数重置指令混淆，不参与匹配
            if isinstance(alias, str) and alias and not alias.isdigit():
                names.setdefault(alias, name)
    return names


def _arcade(entry: dict) -> tuple[int, str, str]:
    """返回 (当前人数, 最后更新人, 最后更新时间)。"""
    deltas = entry.get("deltas", [])
    total = sum(int(x) for x in deltas if isinstance(x, (int, float)))
    return total, entry.get("by", ""), entry.get("at", "")


def _now() -> str:
    return time.strftime("%m-%d %H:%M")


def _allowed(group_id: int) -> bool:
    """所有群默认生效；只有显式列进 disabled_groups 的群才跳过。"""
    return str(group_id) not in config.disabled_groups


def _nickname(event: GroupMessageEvent) -> str:
    return event.sender.card or event.sender.nickname or str(event.user_id)


# --------------------------------------------------------------------------
# 人数上报
# --------------------------------------------------------------------------

_OP_RE = re.compile(r"^(?P<name>.+?)(?P<op>\+\+|--|\+|-|=)(?P<num>\d*)$")
_SET_RE = re.compile(r"^(?P<name>.+?)(?P<num>\d+)$")
# 单独的 = 语义是「重置为 N」，和 +N/-N 分开处理，避免 =7 被当成 -7
_ONLY_NUM_RE = re.compile(r"^(?P<name>.+?)(?P<num>\d*)$")


def _match_update(text: str, known: set[str]) -> tuple[str, str, int] | None:
    """解析人数更新指令，返回 (机厅名, 动作, 数值)。"""
    text = text.strip()

    # <机厅名>=N —— 重置
    m = re.match(r"^(?P<name>.+?)=(?P<num>\d+)$", text)
    if m and m.group("name").strip() in known:
        return m.group("name").strip(), "set", int(m.group("num"))

    m = _OP_RE.match(text)
    if m:
        name = m.group("name").strip()
        op = m.group("op")
        if name in known and op != "=":
            num = m.group("num")
            if op == "++":
                return name, "add", 1
            if op == "--":
                return name, "add", -1
            if num:
                return name, "add", int(num) if op == "+" else -int(num)
            return None

    m = _SET_RE.match(text)
    if m:
        name = m.group("name").strip()
        if name in known:
            return name, "set", int(m.group("num"))
    return None


async def _is_update(event: GroupMessageEvent) -> bool:
    if not _allowed(event.group_id):
        return False
    known = set(_name_map(event.group_id))
    if not known:
        return False
    return _match_update(event.get_plaintext(), known) is not None


arcade_update = on_message(Rule(_is_update), priority=15, block=True)


@arcade_update.handle()
async def handle_update(event: GroupMessageEvent):
    names = _name_map(event.group_id)
    parsed = _match_update(event.get_plaintext(), set(names))
    if not parsed:
        return
    name, action, value = parsed
    name = names.get(name, name)
    gd = _group_data(event.group_id)
    entry = gd.setdefault(name, {"deltas": [], "by": "", "at": ""})

    if action == "set":
        entry["deltas"] = [value]
    else:
        if value > 0:
            entry["deltas"].append(value)
        else:
            # 负数直接记一条 -1，不再像原版那样 pop 掉一整条正数记录
            entry["deltas"].append(value)
        total = sum(int(x) for x in entry["deltas"] if isinstance(x, (int, float)))
        if total < 0:
            entry["deltas"] = [0]

    entry["by"] = _nickname(event)
    entry["at"] = _now()
    _save()

    total, by, at = _arcade(entry)
    verb = "重置为" if action == "set" else "更新为"
    await arcade_update.finish(f"[{name}] 当前人数{verb} {total}\n由 {by} 于 {at} 更新")


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------

_QUERY_SUFFIX = ("几", "几人", "j")


async def _is_query(event: GroupMessageEvent) -> bool:
    if not _allowed(event.group_id):
        return False
    text = event.get_plaintext().strip()
    if not text or text in ("机厅人数",):
        return False
    for suf in _QUERY_SUFFIX:
        if text.endswith(suf) and len(text) > len(suf):
            name = text[: -len(suf)].strip()
            if name in _name_map(event.group_id):
                return True
    return False


arcade_query = on_message(Rule(_is_query), priority=15, block=True)


@arcade_query.handle()
async def handle_query(event: GroupMessageEvent):
    names = _name_map(event.group_id)
    text = event.get_plaintext().strip()
    for suf in _QUERY_SUFFIX:
        if text.endswith(suf):
            name = text[: -len(suf)].strip()
            name = names.get(name, name)
            entry = _group_data(event.group_id).get(name)
            if not entry:
                continue
            total, by, at = _arcade(entry)
            if not by:
                await arcade_query.finish(f"[{name}] 今日人数尚未更新")
            await arcade_query.finish(f"[{name}] 当前人数为 {total}\n由 {by} 于 {at} 更新")
            return


# 注意：不要给这里加 "mai" 别名 —— Rikka 的舞萌指令头就是 /mai，
# 两者同时注册会导致 /mai b50 被机厅插件抢先匹配并回复「今天没有任何机厅人数被更新过」。
show_all = on_command("机厅人数", priority=10, block=True)


@show_all.handle()
async def handle_show_all(event: GroupMessageEvent):
    if not _allowed(event.group_id):
        return
    gd = _group_data(event.group_id)
    lines = []
    for name, entry in gd.items():
        total, by, at = _arcade(entry)
        if not by:
            continue
        lines.append(f"[{name}] 当前人数为 {total}\n由 {by} 于 {at} 更新")
    if not lines:
        await show_all.finish("今天没有任何机厅人数被更新过")
    await show_all.finish("\n\n".join(lines))


# --------------------------------------------------------------------------
# 机厅管理
# --------------------------------------------------------------------------

add_arcade = on_command("添加机厅", priority=10, block=True)
del_arcade = on_command("删除机厅", priority=10, block=True)
list_arcade = on_command("机厅列表", priority=10, block=True)
alias_arcade = on_command("添加机厅别名", priority=10, block=True)


def _parse_alias(text: str, gd: dict) -> tuple[str, str] | None:
    """解析「<机厅名><别名>」，支持空格/逗号分隔或直接拼接。"""
    parts = [p for p in re.split(r"[\s,，]+", text.strip()) if p]
    if len(parts) >= 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        raw = parts[0]
        # 无分隔时按最长已知机厅名切分，避免「天」抢了「天空之城」
        for name in sorted(gd, key=len, reverse=True):
            if raw.startswith(name) and len(raw) > len(name):
                return name, raw[len(name):]
    return None


def _is_admin(event: GroupMessageEvent) -> bool:
    return event.sender.role in ("admin", "owner")


@add_arcade.handle()
async def handle_add(event: GroupMessageEvent, arg: Message = CommandArg()):
    if not _allowed(event.group_id):
        return
    if not _is_admin(event):
        await add_arcade.finish("只有管理员可以添加机厅")
    names = [n.strip() for n in arg.extract_plain_text().replace("，", ",").split(",")]
    names = [n for n in names if n]
    if not names:
        await add_arcade.finish("用法：/添加机厅 <机厅名>，多个用逗号分隔")
    gd = _group_data(event.group_id)
    added, exists = [], []
    for n in names:
        if n in gd:
            exists.append(n)
        else:
            gd[n] = {"deltas": [], "by": "", "at": ""}
            added.append(n)
    _save()
    msg = ""
    if added:
        msg += "已添加：" + "、".join(added)
    if exists:
        msg += ("\n" if msg else "") + "已存在：" + "、".join(exists)
    await add_arcade.finish(msg)


@del_arcade.handle()
async def handle_del(event: GroupMessageEvent, arg: Message = CommandArg()):
    if not _allowed(event.group_id):
        return
    if not _is_admin(event):
        await del_arcade.finish("只有管理员可以删除机厅")
    name = arg.extract_plain_text().strip()
    if not name:
        await del_arcade.finish("用法：/删除机厅 <机厅名>")
    gd = _group_data(event.group_id)
    if name not in gd:
        await del_arcade.finish(f"没有找到机厅「{name}」")
    gd.pop(name, None)
    _save()
    await del_arcade.finish(f"已删除机厅「{name}」")


@list_arcade.handle()
async def handle_list(event: GroupMessageEvent):
    if not _allowed(event.group_id):
        return
    gd = _group_data(event.group_id)
    if not gd:
        await list_arcade.finish("本群还没有添加任何机厅，管理员可用 /添加机厅 添加")
    parts = []
    for name, entry in gd.items():
        total, by, at = _arcade(entry)
        parts.append(f"{name}：{total} 人" + (f"（{by} {at}）" if by else ""))
    await list_arcade.finish("本群机厅：\n" + "\n".join(parts))


@alias_arcade.handle()
async def handle_alias(event: GroupMessageEvent, arg: Message = CommandArg()):
    if not _allowed(event.group_id):
        return
    if not _is_admin(event):
        await alias_arcade.finish("只有管理员可以添加机厅别名")
    text = arg.extract_plain_text().strip()
    gd = _group_data(event.group_id)
    parsed = _parse_alias(text, gd) if text else None
    if not parsed:
        await alias_arcade.finish(
            "用法：/添加机厅别名 <机厅名><别名>，例如 /添加机厅别名 木屋mw"
        )
    name, alias = parsed
    if name not in gd:
        await alias_arcade.finish(f"没有找到机厅「{name}」，可先用 /机厅列表 查看")
    if alias == name:
        await alias_arcade.finish("别名不能和机厅名相同")
    if alias.isdigit():
        await alias_arcade.finish("别名不能是纯数字，避免和人数指令混淆")
    for other, entry in gd.items():
        aliases = entry.get("alias_list")
        aliases = aliases if isinstance(aliases, list) else []
        if alias == other:
            await alias_arcade.finish(f"「{alias}」已经是机厅名，不能用作别名")
        if alias in aliases:
            if other == name:
                await alias_arcade.finish(f"机厅「{name}」已经有别名「{alias}」了")
            await alias_arcade.finish(f"别名「{alias}」已被机厅「{other}」使用")
    entry = gd[name]
    alias_list = entry.get("alias_list")
    if not isinstance(alias_list, list):
        alias_list = []
        entry["alias_list"] = alias_list
    alias_list.append(alias)
    _save()
    await alias_arcade.finish(f"已为机厅「{name}」添加别名「{alias}」")


# --------------------------------------------------------------------------
# 定时清零（0 点）
# --------------------------------------------------------------------------


def _reset_all() -> None:
    """把所有机厅人数清零并立即写回文件。"""
    data = _load()
    for gd in data.values():
        for entry in gd.values():
            entry["deltas"] = []
            entry["by"] = ""
            entry["at"] = ""
    _save()


try:
    from nonebot_plugin_apscheduler import scheduler

    @scheduler.scheduled_job("cron", hour=0, minute=0, id="arcade_daily_reset")
    async def _daily_reset() -> None:
        _reset_all()
        logger.info("[机厅] 已执行 0 点人数清零")

except Exception as e:
    logger.warning(f"[机厅] 未能注册定时清零任务: {e}")
