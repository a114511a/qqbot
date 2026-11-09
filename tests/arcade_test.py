"""机厅人数插件功能测试（纯逻辑，不需要启动 NoneBot / 连接 QQ）。

覆盖：
  1. 指令解析 _match_update（++/--/+N/-N/=N/N、未知机厅、空格、负数保护）
  2. 存储层 deltas 累加语义与 _arcade 求和
  3. 每日清零
  4. 查询后缀识别
  5. 群功能默认对所有群生效（disabled_groups 语义）
  6. "mai" 不再作为机厅别名（避免与 /mai b50 冲突）

运行：
    ./.venv/bin/python tests/arcade_test.py
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "plugins"))

# 直接按文件加载，避开 __init__.py 里 on_command 需要 NoneBot 已初始化的限制
# 先造一个最小 NoneBot 环境让顶层 import 能过
import nonebot  # noqa: E402

nonebot.init()

spec = importlib.util.spec_from_file_location(
    "mai_arcade_under_test", PROJECT_ROOT / "src" / "plugins" / "mai_arcade" / "__init__.py"
)
arc = importlib.util.module_from_spec(spec)
sys.modules["mai_arcade_under_test"] = arc
spec.loader.exec_module(arc)

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"[OK] {label}")
    else:
        FAIL += 1
        print(f"[FAIL] {label}: got={got!r}, want={want!r}")


def check_true(label: str, cond: bool) -> None:
    check(label, bool(cond), True)


# ---------------------------------------------------------------------------
# 1. 指令解析
# ---------------------------------------------------------------------------
KNOWN = {"星际传奇", "风云再起", "环游嘉年华"}


def parse(text):
    return arc._match_update(text, KNOWN)


check("++ 加 1", parse("星际传奇++"), ("星际传奇", "add", 1))
check("-- 减 1", parse("星际传奇--"), ("星际传奇", "add", -1))
check("+3 加 3", parse("星际传奇+3"), ("星际传奇", "add", 3))
check("-2 减 2", parse("星际传奇-2"), ("星际传奇", "add", -2))
check("=7 重置为 7", parse("星际传奇=7"), ("星际传奇", "set", 7))
check("N 直接写数字 = 重置", parse("星际传奇5"), ("星际传奇", "set", 5))
check("前后空格", parse("  风云再起 ++  "), ("风云再起", "add", 1))
check("中文机厅名 +N", parse("环游嘉年华+10"), ("环游嘉年华", "add", 10))
check("未知机厅 ++ 不触发", parse("不存在++"), None)
check("未知机厅 =N 不触发", parse("不存在=7"), None)
check("纯数字消息不触发", parse("12345"), None)
check("普通聊天不触发", parse("今天天气真好"), None)
check("只有 + 没有数字/++ 不触发", parse("星际传奇+"), None)
check("= 后无数字不触发", parse("星际传奇="), None)
check("-- 不会被 _SET_RE 误判成 set", parse("星际传奇--")[1], "add")

# ---------------------------------------------------------------------------
# 2. 存储层：deltas 累加 / _arcade 求和 / set 重置
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    arc.DATA_FILE = Path(td) / "arcade.json"
    arc._data = {}
    arc._loaded = True

    gd = arc._group_data(123456)
    entry = gd.setdefault("星际传奇", {"deltas": [], "by": "", "at": ""})

    # 模拟 ++ 三次、-- 一次
    for v in (1, 1, -1):
        entry["deltas"].append(v)
    total, by, at = arc._arcade(entry)
    check("deltas [1,1,-1] 求和为 1", total, 1)

    # 再 +3 → [1,1,-1,3] = 4
    entry["deltas"].append(3)
    check("deltas 累加 +3 后为 4", arc._arcade(entry)[0], 4)

    # set 重置
    entry["deltas"] = [7]
    check("set 重置后为 7", arc._arcade(entry)[0], 7)

    # _save / 重新 _load 持久化
    entry["by"] = "测试员"
    entry["at"] = "11-08 22:00"
    arc._save()
    check("保存后文件存在", arc.DATA_FILE.exists(), True)
    arc._loaded = False
    arc._data = {}
    reloaded = arc._load()
    check("重新加载后人数保持", arc._arcade(reloaded["123456"]["星际传奇"])[0], 7)
    check("重新加载后更新人保持", reloaded["123456"]["星际传奇"]["by"], "测试员")

    # 0 点定时清零走 _reset_all
    arc._reset_all()
    check("清零后人数为 0", arc._arcade(reloaded["123456"]["星际传奇"])[0], 0)
    check("清零后更新人为空", reloaded["123456"]["星际传奇"]["by"], "")
    arc._loaded = False
    arc._data = {}
    cleared = arc._load()
    check("清零结果已落盘", arc._arcade(cleared["123456"]["星际传奇"])[0], 0)

# ---------------------------------------------------------------------------
# 3. 查询后缀识别所需的数据存在性（_group_data 按群隔离）
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    arc.DATA_FILE = Path(td) / "arcade.json"
    arc._data = {}
    arc._loaded = True
    g1 = arc._group_data(111)
    g1["A"] = {"deltas": [2], "by": "u1", "at": "t"}
    g2 = arc._group_data(222)
    g2["B"] = {"deltas": [5], "by": "u2", "at": "t"}
    check("群隔离：111 只有 A", set(arc._group_data(111).keys()), {"A"})
    check("群隔离：222 只有 B", set(arc._group_data(222).keys()), {"B"})
    check("A 人数 2", arc._arcade(arc._group_data(111)["A"])[0], 2)
    check("B 人数 5", arc._arcade(arc._group_data(222)["B"])[0], 5)

# ---------------------------------------------------------------------------
# 3.5 别名：名称映射、别名触发、/添加机厅别名 参数解析
# ---------------------------------------------------------------------------
with tempfile.TemporaryDirectory() as td:
    arc.DATA_FILE = Path(td) / "arcade_data.json"
    arc._data = {}
    arc._loaded = True
    gd = arc._group_data(333)
    gd["木屋"] = {"deltas": [], "by": "", "at": "", "alias_list": ["mw"]}
    gd["大玩家"] = {"deltas": [], "by": "", "at": ""}
    gd["天空之城"] = {"deltas": [], "by": "", "at": "", "alias_list": ["tk"]}
    gd["天"] = {"deltas": [], "by": "", "at": ""}
    gd["xx"] = {"deltas": [], "by": "", "at": "", "alias_list": ["1"]}

    names = arc._name_map(333)
    check("别名映射到正式名", names.get("mw"), "木屋")
    check("正式名映射自身", names.get("木屋"), "木屋")
    check("纯数字别名不参与匹配", names.get("1"), None)
    check("别名可触发 +1", arc._match_update("mw++", set(names)), ("mw", "add", 1))
    check("别名可触发 +N", arc._match_update("tk+3", set(names)), ("tk", "add", 3))
    check("未知别名不触发", arc._match_update("zz++", set(names)), None)

    check("直接拼接解析", arc._parse_alias("大玩家dpj", gd), ("大玩家", "dpj"))
    check("空格分隔解析", arc._parse_alias("大玩家 dpj", gd), ("大玩家", "dpj"))
    check("逗号分隔解析", arc._parse_alias("木屋,木头", gd), ("木屋", "木头"))
    check("最长机厅名优先", arc._parse_alias("天空之城tt", gd), ("天空之城", "tt"))
    check("缺少别名解析失败", arc._parse_alias("木屋", gd), None)
    check("未知机厅解析失败", arc._parse_alias("无名xx", gd), None)
    check("空参数解析失败", arc._parse_alias("  ", gd), None)

    # 保存后别名可持久化
    arc._save()
    arc._loaded = False
    arc._data = {}
    reloaded = arc._load()
    check("别名持久化", reloaded["333"]["木屋"]["alias_list"], ["mw"])

# ---------------------------------------------------------------------------
# 4. 群功能默认对所有群生效
# ---------------------------------------------------------------------------
class _Cfg:
    disabled_groups: list[str] = []


orig_cfg = arc.config
arc.config = _Cfg()
check_true("默认所有群允许", all(arc._allowed(g) for g in (111, 222, 999999, 790837113)))
arc.config.disabled_groups = ["222"]
check("排除名单内被拦截", arc._allowed(222), False)
check("排除名单外仍允许", arc._allowed(111), True)
arc.config = orig_cfg

# ---------------------------------------------------------------------------
# 5. "mai" 不再是机厅别名（防止 /mai b50 被重复触发）
# ---------------------------------------------------------------------------
src = (PROJECT_ROOT / "src" / "plugins" / "mai_arcade" / "__init__.py").read_text(encoding="utf-8")
check_true('源码未注册 aliases={"mai"}', 'aliases={"mai"}' not in src)
check_true('注释明确禁止 mai 别名', "不要给这里加 \"mai\" 别名" in src)

print(f"\n共 {PASS + FAIL} 项检查，PASS {PASS}，FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
