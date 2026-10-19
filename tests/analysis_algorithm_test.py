"""底力分析算法回归测试（离线，不联网）。

现有两个测试覆盖的是「流程能不能跑通」：
- tests/smoke_test.py          插件加载 + 指令匹配
- tests/binding_flow_test.py   真实 API 的绑定→出报告→解绑

它们都**测不出算法退化**。本文件专门守住算法的统计不变量，
这些都是实测踩过坑之后才定下来的性质，改坏了必须立刻报警：

1. 推算定数校准后，残差不能和定数相关（不校准会相关到 0.9 以上，
   把所有早期低定数老谱误判成诈称）；
2. 各定数档的残差偏置必须接近 0；
3. 纯随机数据下五维能力倾向与九维配置维度都必须基本「不显著」
   （算法不能从噪声里编倾向）；
4. 真实数据下能识别出已知的强倾向方向；
5. 配置 / 能力载荷必须有限、非负，维度定义与实现一致；
6. 样本不足 / 空目录 / 无评级等边界不能崩，也不能把缓存冲掉；
7. 难度坎只在数据真掉下来的时候才认，平的时候不许硬编。

运行：
    ./.venv/bin/python tests/analysis_algorithm_test.py
"""

from __future__ import annotations

import importlib.util
import json
import math
import random
import sys
import types
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# analysis.py 通过 `from .lxns import ...` 引用同包的落雪请求层，
# 所以要把 src/plugins 放进 sys.path，让 chunithm_bot 作为真正的包被导入，
# 相对导入才有父包可寻。
PLUGIN_DIR = PROJECT_ROOT / "src" / "plugins"
SONG_CACHE = PROJECT_ROOT / "data" / "chunithm_songs_cache.json"

# 这个测试只测纯计算，不需要 NoneBot。但 analysis.py 现在用 `from .lxns import ...`，
# 必须先有父包 chunithm_bot 在 sys.modules 里才能解析相对导入；而正常导入包又会执行
# __init__.py（里面 import commands，需要 NoneBot 已初始化）并触发 on_command 报错。
#
# 解法：先放一个空的包壳占住 sys.modules，再按文件路径加载 analysis.py。
# analysis.py 里的相对导入会命中这个壳的 __path__，直接找到 lxns.py，全程不碰 NoneBot。
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

_pkg_name = "chunithm_bot"
if _pkg_name not in sys.modules:
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = [str(PLUGIN_DIR / _pkg_name)]  # 让 `from .lxns import` 可解析
    _pkg.__package__ = _pkg_name
    sys.modules[_pkg_name] = _pkg

ANALYSIS_PATH = PLUGIN_DIR / _pkg_name / "analysis.py"
_spec = importlib.util.spec_from_file_location(f"{_pkg_name}.analysis", ANALYSIS_PATH)
analysis = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules[_spec.name] = analysis
_spec.loader.exec_module(analysis)

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"[OK] {label}" + (f"  {detail}" if detail else ""))
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = math.sqrt(sum((a - mx) ** 2 for a in xs))
    vy = math.sqrt(sum((b - my) ** 2 for b in ys))
    return cov / (vx * vy) if vx and vy else 0.0


def load_catalog() -> dict:
    if not SONG_CACHE.exists():
        print(f"缺少曲目缓存 {SONG_CACHE}，跳过需要目录的用例。")
        return {"songs": [], "versions": []}
    return json.loads(SONG_CACHE.read_text(encoding="utf-8"))


def build_mapped(catalog: dict, scores: list[dict]) -> list[dict]:
    """复用生产代码的映射逻辑，构造 compute_ability_profile 的输入。"""
    report = analysis.compute_report({"name": "算法测试", "rating": 15.0}, scores, catalog)
    # compute_report 内部已经算过一次 ability，这里只取 mapped 不方便，
    # 所以用 fit_rows 反推一份带 notes 的 mapped（fit_rows 就是 mapped 的超集）。
    return report["fit_rows"]


def make_synthetic_scores(catalog: dict, count: int, seed: int) -> list[dict]:
    """用固定种子造一批合成成绩：评级随机、与谱面特征无关。"""
    rng = random.Random(seed)
    charts: list[tuple[int, dict]] = []
    for song in catalog.get("songs", []):
        for chart in song.get("difficulties", []) or []:
            notes = chart.get("notes") or {}
            if int(chart.get("difficulty", -1)) == 3 and notes.get("total"):
                charts.append((int(song["id"]), chart))
    rng.shuffle(charts)
    ranks = ["sssp", "sss", "ssp", "ss", "sp", "s"]
    return [
        {
            "id": song_id,
            "level_index": 3,
            "rank": rng.choices(ranks, weights=[6, 14, 26, 30, 18, 6])[0],
            "clear": "clear",
            "song_name": "synthetic",
        }
        for song_id, _ in charts[:count]
    ]


def test_rank_table() -> None:
    """评级区间中点表必须是单调递增的完整阶梯。"""
    print("\n=== 1. 评级→分数中点表 ===")
    order = ["d", "c", "b", "bb", "bbb", "a", "aa", "aaa", "s", "sp", "ss", "ssp", "sss", "sssp"]
    table = analysis.RANK_SCORE_MIDPOINT
    check("覆盖全部 14 个评级", set(table) == set(order), f"缺失 {set(order) - set(table)}")
    values = [table[r] for r in order]
    check("单调递增", all(b > a for a, b in zip(values, values[1:])))
    check("不超过满分", max(values) < analysis.MAX_SCORE, f"max={max(values)}")
    # 与真实分数校验过的区间下界一致（见 analysis.py 注释）：中点必须落在自己的区间里。
    lower = {
        "d": 0, "c": 500000, "b": 600000, "bb": 700000, "bbb": 800000,
        "a": 900000, "aa": 925000, "aaa": 950000, "s": 975000, "sp": 990000,
        "ss": 1000000, "ssp": 1005000, "sss": 1007500, "sssp": 1009000,
    }
    for index, rank in enumerate(order):
        low = lower[rank]
        high = lower[order[index + 1]] if index + 1 < len(order) else int(analysis.MAX_SCORE)
        check(f"{rank} 中点落在 [{low}, {high})", low <= table[rank] < high, f"中点 {table[rank]}")


def test_fit_calibration(catalog: dict) -> None:
    """推算定数：校准后残差必须与定数无关、各档偏置接近 0。"""
    print("\n=== 2. 推算定数校准 ===")
    if not catalog.get("songs"):
        check("曲目目录可用", False, "缓存缺失")
        return
    fit = analysis.build_fit_constants(catalog)
    check("生成了推算值", len(fit["charts"]) > 5000, f"{len(fit['charts'])} 张")
    check("各档都产出精度指标", {"MASTER", "EXPERT"} <= set(fit["tiers"]))

    # 逐档检查残差-定数相关性。这是校准是否生效的核心判据：
    # 不校准的话 MASTER 档这个相关系数会到 0.6 以上。
    by_tier: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for payload in fit["charts"].values():
        by_tier[payload["tier"]].append((payload["official_constant"], payload["residual"]))
    for tier, rows in sorted(by_tier.items()):
        if len(rows) < 200:
            continue
        corr = pearson([r[0] for r in rows], [r[1] for r in rows])
        check(f"{tier} 残差与定数不相关", abs(corr) < 0.05, f"r={corr:+.3f}（n={len(rows)}）")

    # 分档偏置：按 0.25 一档算平均残差，应该都接近 0。
    for tier in ("MASTER", "EXPERT"):
        rows = by_tier.get(tier) or []
        bands: dict[float, list[float]] = defaultdict(list)
        for level, residual in rows:
            bands[round(level * 4) / 4].append(residual)
        worst = max(
            (abs(sum(v) / len(v)), band, len(v)) for band, v in bands.items() if len(v) >= 10
        )
        check(f"{tier} 档位偏置接近 0", worst[0] < 0.01, f"最大 {worst[0]:.4f}（档位 {worst[1]}, n={worst[2]}）")

    # z 值分布：|z|>=2 的比例应该接近正态的 4.5%，不能爆掉。
    for tier in ("MASTER", "EXPERT"):
        zs = [p["z"] for p in fit["charts"].values() if p["tier"] == tier]
        rate = sum(1 for z in zs if abs(z) >= 2) / len(zs)
        check(f"{tier} 标记率合理", 0.01 <= rate <= 0.12, f"{rate * 100:.1f}%")

    # 精度不能退化（当前实测 MASTER R²=0.694 / σ=0.609）
    master = fit["tiers"]["MASTER"]
    check("MASTER R² 未退化", master["r2"] > 0.60, f"R²={master['r2']}")
    check("MASTER 残差 σ 未退化", master["residual_std"] < 0.80, f"σ={master['residual_std']}")
    check("原始预测确实被压缩（校准有必要）", master["shrink_ratio"] < 0.75, f"压缩比={master['shrink_ratio']}")


def test_ability_on_noise(catalog: dict) -> None:
    """纯随机成绩下，五维能力倾向与九维配置维度都必须基本不显著。

    算法不能从噪声里编倾向。配置维度之间载荷高度相关，同一个随机波动会同时
    打到好几个维度上，所以对九维的容忍度略高于五维。
    """
    print("\n=== 3. 随机数据不应产生倾向 ===")
    if not catalog.get("songs"):
        check("曲目目录可用", False, "缓存缺失")
        return
    # 用同一份曲目目录建推算表，保证 compute_report 走的是缓存路径
    analysis.build_fit_constants(catalog)
    for seed in (1, 2, 3):
        scores = make_synthetic_scores(catalog, 800, seed)
        mapped = build_mapped(catalog, scores)
        profile = analysis.compute_ability_profile(mapped)
        check(f"seed={seed} 样本量足够", profile["available"], f"n={profile['sample_count']}")
        if not profile["available"]:
            continue
        groups = (
            ("能力倾向", profile["dimensions"], 1),
            ("配置维度", (profile.get("configs") or {}).get("dimensions") or [], 3),
        )
        for label, entries, limit in groups:
            scored = [d for d in entries if d.get("available")]
            significant = [
                d["label"] for d in scored if d.get("significance") in ("明显", "较明显")
            ]
            indices = [d["index"] for d in scored]
            check(
                f"seed={seed} {label}显著维度极少",
                len(significant) <= limit,
                f"显著 {significant or '无'}",
            )
            check(
                f"seed={seed} {label}指数贴近中性",
                max(abs(v - 50) for v in indices) < 30,
                f"最大偏离 {max(abs(v - 50) for v in indices):.1f}",
            )


def test_ability_on_real_signal() -> None:
    """构造一个「TAP 占比越高表现越差」的确定性信号，算法必须识别出来。"""
    print("\n=== 4. 能识别构造出的真实信号 ===")
    catalog = load_catalog()
    if not catalog.get("songs"):
        check("曲目目录可用", False, "缓存缺失")
        return
    analysis.build_fit_constants(catalog)
    charts = []
    for song in catalog["songs"]:
        for chart in song.get("difficulties", []) or []:
            notes = chart.get("notes") or {}
            if int(chart.get("difficulty", -1)) == 3 and notes.get("total"):
                charts.append((int(song["id"]), chart, notes))
    if len(charts) < 300:
        check("MASTER 谱面充足", False, f"{len(charts)} 张")
        return
    # 评级完全由 TAP 占比决定（占比越高评级越低），其余键型不参与。
    ranks = ["sssp", "sss", "ssp", "ss", "sp", "s", "aaa", "aa", "a", "bbb"]
    ordered = sorted(charts, key=lambda item: item[2]["tap"] / item[2]["total"])
    scores = []
    for position, (song_id, _, notes) in enumerate(ordered):
        bucket = min(len(ranks) - 1, position * len(ranks) // len(ordered))
        scores.append(
            {"id": song_id, "level_index": 3, "rank": ranks[bucket], "clear": "clear", "song_name": "signal"}
        )
    profile = analysis.compute_ability_profile(build_mapped(catalog, scores))
    axes = {d["key"]: d for d in profile["dimensions"] if d.get("available")}
    keyboard = axes.get("ax_keyboard")
    check("键盘谱维度可用", keyboard is not None)
    if keyboard:
        check("键盘谱被判为偏弱", keyboard["index"] < 30, f"指数={keyboard['index']}")
        check("键盘谱差异显著", keyboard["significance"] in ("明显", "较明显"), f"{keyboard['significance']}")
        check("键盘谱差值为负", keyboard["delta_score"] < 0, f"{keyboard['delta_score']:+d} 分")

    # 配置维度里由 TAP 主导的几个必须同向为负——信号只由 TAP 占比驱动。
    configs = {
        d["key"]: d
        for d in (profile.get("configs") or {}).get("dimensions", [])
        if d.get("available")
    }
    tap_driven = [key for key in ("trill", "chain", "speed", "stream") if key in configs]
    check("TAP 主导的配置维度可算", len(tap_driven) >= 3, f"{tap_driven}")
    if tap_driven:
        check(
            "TAP 主导的配置维度同向为负",
            all(configs[key]["delta_score"] < 0 for key in tap_driven),
            "、".join(f"{configs[key]['label']}{configs[key]['delta_score']:+d}" for key in tap_driven),
        )


def test_ability_sample_gate() -> None:
    """样本不足时必须明说，而不是硬算出一个结论。"""
    print("\n=== 5. 样本量门槛 ===")
    profile = analysis.compute_ability_profile([])
    check("空输入不可用", not profile["available"])
    check("空输入给出原因", bool(profile.get("reason")), profile.get("reason", "")[:40])

    # 造 30 张（少于 ABILITY_MIN_SAMPLES=60）
    catalog = load_catalog()
    fake = [
        {
            "title": f"t{i}",
            "difficulty": "MASTER",
            "level": "14",
            "level_value": 14.0,
            "rank": "sss",
            "clear": "clear",
            "notes": {"total": 1500, "tap": 900, "hold": 200, "slide": 200, "air": 150, "flick": 50},
        }
        for i in range(30)
    ]
    profile = analysis.compute_ability_profile(fake)
    check("30 张判为样本不足", not profile["available"], f"n={profile['sample_count']}")
    check("30 张配置维度也不可用", not (profile.get("configs") or {}).get("available"))


def test_style_loads(catalog: dict) -> None:
    """配置 / 能力载荷必须有限、非负，且维度定义与实现一一对应。"""
    print("\n=== 8. 配置与能力载荷 ===")
    config_keys = [key for key, _, _ in analysis.CONFIG_DIMENSIONS]
    axis_keys = [key for key, _, _ in analysis.ABILITY_AXES]
    check("配置维度 9 个", len(config_keys) == 9, str(config_keys))
    check("能力倾向 5 个", len(axis_keys) == 5, str(axis_keys))
    check("配置键无重复", len(set(config_keys)) == len(config_keys))
    check("能力键无重复", len(set(axis_keys)) == len(axis_keys))
    # 两套键不能撞名，否则样本字段会互相覆盖。
    check("两套键不重名", not (set(config_keys) & set(axis_keys)))

    # 熵：单一构成 → 0，均匀构成 → 1。
    check("熵：单值构成", abs(analysis._entropy([1, 0, 0, 0, 0])) < 1e-9)
    check("熵：均匀构成", abs(analysis._entropy([1, 1, 1, 1, 1]) - 1.0) < 1e-9)

    if not catalog.get("songs"):
        check("曲目目录可用", False, "缓存缺失")
        return
    expected = set(config_keys) | set(axis_keys)
    checked = skipped = 0
    bad: list[str] = []
    for song in catalog["songs"]:
        for chart in song.get("difficulties", []) or []:
            if not (chart.get("notes") or {}).get("total"):
                continue
            loads = analysis._style_loads(chart)
            if loads is None:
                skipped += 1
                continue
            checked += 1
            if set(loads) != expected:
                bad.append("键不一致")
                continue
            for key, value in loads.items():
                if not math.isfinite(value) or value < 0:
                    bad.append(f"{key}={value}")
    check(
        "全部谱面载荷有限且非负",
        not bad,
        f"检查 {checked} 张（跳过 {skipped} 张缺定数），异常 {bad[:3]}",
    )


def test_render_and_cache_edges(catalog: dict) -> None:
    """边界输入不能崩，也不能把推算定数缓存冲掉。"""
    print("\n=== 6. 边界与缓存保护 ===")
    # 用 ensure_fit_constants 保证缓存文件确实落盘，否则后面的对比没有意义。
    analysis.ensure_fit_constants(catalog)
    before = analysis.FIT_PATH.read_text(encoding="utf-8") if analysis.FIT_PATH.exists() else ""
    check("缓存已建立", len(before) > 100_000, f"{len(before)} 字节")

    # 空目录：不能写盘覆盖
    analysis.ensure_fit_constants({"songs": [], "versions": []})
    after = analysis.FIT_PATH.read_text(encoding="utf-8") if analysis.FIT_PATH.exists() else ""
    check("空目录未冲掉缓存", len(after) >= len(before), f"{len(before)} → {len(after)}")

    cases = [
        ("两条成绩", {"name": "A", "rating": 15.0}, [
            {"id": 2563, "level_index": 3, "rank": "sssp", "clear": "hard", "song_name": "x"},
            {"id": 2764, "level_index": 3, "rank": "ssp", "clear": "hard", "song_name": "y"},
        ]),
        ("全部失败", {"name": "B", "rating": 14.0}, [
            {"id": 2563, "level_index": 3, "rank": "d", "clear": "failed", "song_name": "z"},
        ] * 3),
        ("无 Rating", {"name": "C"}, [
            {"id": 2563, "level_index": 3, "rank": "sss", "clear": "clear", "song_name": "w"},
        ]),
        ("超长玩家名", {"name": "名" * 40, "rating": 16.0}, [
            {"id": 3, "level_index": 3, "rank": "sssp", "clear": "hard", "song_name": "v"},
        ]),
    ]
    for label, player, scores in cases:
        try:
            report = analysis.compute_report(player, scores, catalog)
            report["summary"] = analysis.local_summary(report)
            path = analysis.render_report(report)
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            check(f"{label} 可渲染", size > 50_000, f"{size} 字节")
        except Exception as exc:  # noqa: BLE001 - 测试里就是要抓所有异常
            check(f"{label} 可渲染", False, f"{type(exc).__name__}: {exc}")

    # 空目录 + 成绩：应全部计入未匹配，而不是崩
    try:
        report = analysis.compute_report(
            {"name": "D", "rating": 13.0},
            [{"id": 2563, "level_index": 3, "rank": "sss", "clear": "clear", "song_name": "u"}],
            {"songs": [], "versions": []},
        )
        check("空曲目目录不崩", report["mapped_total"] == 0 and report["missing_actual"] == 1)
    except Exception as exc:  # noqa: BLE001
        check("空曲目目录不崩", False, f"{type(exc).__name__}: {exc}")


def test_wrap_keeps_ascii_words() -> None:
    """折行不能把英文单词切开（实测 MASTER 被切成 MAST/ER）。"""
    print("\n=== 7. 折行不切断英文词 ===")
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (600, 100)))
    font = analysis._font(17)
    text = "定数偏离由谱面物量推算（MASTER 档残差 ±0.609），不是跨玩家拟合定数。"
    lines = analysis._wrap(draw, text, font, 300)
    check("折行结果非空", bool(lines))
    for line in lines[:-1]:
        check(f"行尾未截断英文：…{line[-6:]}", not line.rstrip().endswith(("MAST", "MAS", "MA")), line)
    joined = "".join(lines).replace(" ", "")
    check("内容未丢失", "MASTER" in joined, joined[:40])
    # 避头尾：句号逗号不能出现在行首（曾经出现过单独一行的「。」）
    bad = [line for line in lines if line[:1] in analysis.NO_LINE_START]
    check("收尾标点不在行首", not bad, str(bad))


def test_difficulty_cliff() -> None:
    """难度坎：SSS 及以上占比掉得最狠的那一对相邻档必须被认出来。

    总结文案里「14+ 一路碾、一碰 15 就手软」全靠这个判断，
    所以既要认得出真坎，也不能在数据本来就平的时候硬编一个坎出来。
    """
    print("\n=== 9. 难度坎识别 ===")
    keys = [analysis._level_sort_key(x) for x in ("13", "13+", "14", "14+", "15")]
    check("等级排序正确", keys == [13.0, 13.5, 14.0, 14.5, 15.0], str(keys))
    check(
        "非法等级返回 None",
        analysis._level_sort_key("") is None and analysis._level_sort_key("abc") is None,
    )

    def rows(level: str, count: int, sss: int) -> list[dict]:
        return [
            {"level": level, "rank": "sss" if i < sss else "ss", "clear": "clear", "score": 0}
            for i in range(count)
        ]

    # 14+ 大半 SSS，15 只剩零星 → 坎应该落在 14+ → 15
    mapped = rows("14", 40, 20) + rows("14+", 40, 30) + rows("15", 40, 5)
    stats = analysis.level_performance(mapped)
    check("按等级升序", [r["level"] for r in stats] == ["14", "14+", "15"], str([r["level"] for r in stats]))
    check("SSS 以上计数正确（不含 SS+）", stats[1]["sss_count"] == 30, str(stats[1]["sss_count"]))

    # SS+ 是 99 万档，混进「SSS 以上」会把张数虚报出去，玩家一眼看得出来
    with_ssp = rows("14+", 40, 30) + [
        {"level": "14+", "rank": "ssp", "clear": "clear", "score": 0} for _ in range(10)
    ]
    ssp_row = next(r for r in analysis.level_performance(with_ssp) if r["level"] == "14+")
    check(
        "SS+ 不计入 SSS 以上",
        ssp_row["sss_count"] == 30 and ssp_row["count"] == 50,
        f"{ssp_row['sss_count']}/{ssp_row['count']}",
    )
    check("样本量正确", [r["count"] for r in stats] == [40, 40, 40], str([r["count"] for r in stats]))

    report = {"level_stats": stats, "mapped_total": len(mapped)}
    cliff = analysis._difficulty_cliff(report)
    check(
        "坎落在 14+ → 15",
        cliff is not None and cliff[0]["level"] == "14+" and cliff[1]["level"] == "15",
        f"{cliff[0]['level']} → {cliff[1]['level']}" if cliff else "None",
    )
    jab = analysis._difficulty_jab(report)
    check("文案点名 15 且说软", "15" in jab and "软" in jab, jab[:46])
    closing = analysis._closing_line(report)
    check("建议也指向 15", "15" in closing, closing[:46])

    # 上一档直接归零时，「只剩 0 张」读着别扭，应该换成「一张都没摸到 / 归零」
    zero = rows("14+", 40, 30) + rows("15", 40, 0)
    zero_report = {"level_stats": analysis.level_performance(zero), "mapped_total": len(zero)}
    zero_jab = analysis._difficulty_jab(zero_report)
    check("归零时不说「只剩 0 张」", "只剩 0 张" not in zero_jab and "归零" in zero_jab, zero_jab[:40])

    # 各档表现本来就差不多 → 不许硬编一个坎
    flat = rows("14", 40, 30) + rows("14+", 40, 31) + rows("15", 40, 29)
    flat_report = {"level_stats": analysis.level_performance(flat), "mapped_total": len(flat)}
    flat_jab = analysis._difficulty_jab(flat_report)
    check("平坦数据不硬编坎", "软" not in flat_jab, flat_jab[:46])

    # 样本太少（每档 2 张）→ 不做判断，免得一句话建立在两张谱上
    tiny = rows("14", 2, 2) + rows("15", 2, 0)
    tiny_report = {"level_stats": analysis.level_performance(tiny), "mapped_total": len(tiny)}
    check("样本不足不判断", analysis._difficulty_cliff(tiny_report) is None)


def main() -> int:
    catalog = load_catalog()
    print(f"曲目目录：{len(catalog.get('songs', []))} 首")
    test_rank_table()
    test_fit_calibration(catalog)
    test_ability_on_noise(catalog)
    test_ability_on_real_signal()
    test_ability_sample_gate()
    test_render_and_cache_edges(catalog)
    test_wrap_keeps_ascii_words()
    test_style_loads(catalog)
    test_difficulty_cliff()

    print(f"\n共 {CHECKS} 项检查")
    if FAILURES:
        print(f"FAIL: {len(FAILURES)} 项未通过")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("PASS: 算法不变量全部满足")
    return 0


if __name__ == "__main__":
    sys.exit(main())
