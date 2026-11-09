"""补齐舞萌封面：宴会场（utage）谱面复用原曲封面。

舞萌曲库里大量「宴会场」谱面 ID 形如 ``NNNNNN``，其中减去 10000 的整数倍后
会落到一条真实存在的常规曲目上（高位是宴会场分类：``[耐]`` ``[甘]`` ``[J]``
``[r]`` ``[譜]`` ``[龍]`` …）。这些谱面本身没有独立封面，游戏内直接复用原曲
曲绘，所以这里按规则拷贝，而不是去 CDN 下载（CDN 上根本没有这些 ID）。

幂等：目标文件已存在则跳过。
用法：
    ./.venv/bin/python tools/fix_mai_utage_covers.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COVER_DIR = PROJECT_ROOT / "static" / "mai" / "cover"
DB_PATH = PROJECT_ROOT / "data" / "rikka.sqlite3"

# 宴会场分类最多到 16 段（16xxxx），留一点余量
_MAX_SHIFT = 20


def resolve_base(song_id: int, available: set[int]) -> int | None:
    """在 available 里找出 song_id 对应的原曲封面 ID。

    从最近的偏移开始试，命中即返回；偏移量最小 = 最接近的原曲。
    """
    for k in range(1, _MAX_SHIFT + 1):
        cand = song_id - k * 10000
        if cand < 0:
            break
        if cand in available:
            return cand
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印，不实际拷贝")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"找不到数据库: {DB_PATH}")
        return 1

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = con.execute("SELECT id, title FROM nonebot_plugin_rikka_maisong").fetchall()
    con.close()

    existing = {int(p.stem) for p in COVER_DIR.glob("*.png")}
    missing = [(sid, title) for sid, title in rows if sid not in existing]

    print(f"曲库 {len(rows)} 首，已有封面 {len(existing)} 张，缺 {len(missing)} 张")

    fixed = 0
    unresolved: list[tuple[int, str]] = []

    for song_id, title in missing:
        base = resolve_base(song_id, existing)
        if base is None:
            unresolved.append((song_id, title))
            continue
        dst = COVER_DIR / f"{song_id}.png"
        if not args.dry_run:
            dst.write_bytes((COVER_DIR / f"{base}.png").read_bytes())
        fixed += 1

    verb = "可补" if args.dry_run else "已补"
    print(f"{verb} {fixed} 张（复用原曲封面）")

    if unresolved:
        print(f"仍无来源 {len(unresolved)} 张：")
        for sid, title in unresolved[:20]:
            print(f"  {sid}  {title[:45]}")
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
