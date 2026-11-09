"""下载中二节奏成绩图所需的全部静态素材（曲绘 / 名牌底板 / 数字渐变）。

资源来自 assets2.lxns.net，可直连，不需要 Cloudflare challenge。
曲目 ID 取自 ``data/rikka.sqlite3``（需先执行 /update songs 初始化曲目库）。

用法：

    ./.venv/bin/python tools/download_chu_assets.py          # 全量
    ./.venv/bin/python tools/download_chu_assets.py covers   # 只补曲绘
    ./.venv/bin/python tools/download_chu_assets.py plates   # 只补名牌

注意：
- 本脚本只写 ``static/chu/``，该目录被 .gitignore 忽略，不会进仓库。
- 8000~8999 是 WORLDS_END 曲目，LXNS 用 ``id - 8000`` 的原曲封面；
  少数无对应原曲的会生成一张占位图，避免渲染出灰白方块。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
CHU = ROOT / "static" / "chu"
BASE = "https://assets2.lxns.net/chunithm"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


def _is_png(data: bytes) -> bool:
    return bool(data) and data[:8] == b"\x89PNG\r\n\x1a\n"


async def fetch(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"})
        if resp.status_code == 200 and _is_png(resp.content):
            return resp.content
    except Exception:
        pass
    return None


def song_ids() -> list[int]:
    db = ROOT / "data" / "rikka.sqlite3"
    if not db.exists():
        print("未找到 data/rikka.sqlite3，请先运行 /update songs 初始化曲目库")
        return []
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    ids = [r[0] for r in con.execute("SELECT id FROM nonebot_plugin_rikka_chusong")]
    con.close()
    return ids


async def download_covers() -> None:
    out = CHU / "cover"
    out.mkdir(parents=True, exist_ok=True)
    ids = song_ids()
    if not ids:
        return
    missing = [i for i in ids if not (out / f"{i}.png").exists()]
    print(f"曲目 {len(ids)} 首，待下载封面 {len(missing)} 张")

    sem = asyncio.Semaphore(16)
    failed: list[int] = []

    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:

        async def one(i: int) -> None:
            async with sem:
                data = await fetch(client, f"{BASE}/jacket/{i}.png")
                if data is None and 8000 <= i < 9000:
                    data = await fetch(client, f"{BASE}/jacket/{i - 8000}.png")
                if data:
                    (out / f"{i}.png").write_bytes(data)
                else:
                    failed.append(i)

        await asyncio.gather(*(one(i) for i in missing))

    # 少数既无原曲、也无对应封面的，生成占位图，避免渲染成灰白方块
    for i in failed:
        _placeholder(out / f"{i}.png", f"Song {i}")
    print(f"封面完成，新增 {len(missing) - len(failed)} 张，占位图 {len(failed)} 张")


def _placeholder(path: Path, text: str) -> None:
    im = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for y in range(300):
        t = y / 300
        d.line([(0, y), (300, y)], fill=(int(120 + 80 * t), int(110 + 60 * t), int(190 + 50 * t), 255))
    d.rounded_rectangle((18, 18, 282, 282), radius=24, outline=(255, 255, 255, 120), width=3)
    font = None
    for p in (CHU / "font" / "FOT-RodinNTLGPro-B.otf", ROOT / "static" / "font" / "ResourceHanRoundedCN-Bold.ttf"):
        if p.exists():
            try:
                font = ImageFont.truetype(str(p), 24)
                break
            except Exception:
                pass
    if font:
        w = d.textlength(text, font=font)
        d.text(((300 - w) / 2, 136), text, fill=(255, 255, 255, 235), font=font)
    im.save(path)


async def download_plates() -> None:
    out = CHU / "plate"
    out.mkdir(parents=True, exist_ok=True)
    candidates = list(range(1, 41)) + [100, 200, 300]
    todo = [i for i in candidates if not (out / f"{i}.png").exists()]
    print(f"待下载名牌底板 {len(todo)} 张")
    sem = asyncio.Semaphore(12)
    ok = 0

    async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:

        async def one(i: int) -> None:
            nonlocal ok
            async with sem:
                data = await fetch(client, f"{BASE}/plate/{i}.png")
                if data:
                    (out / f"{i}.png").write_bytes(data)
                    ok += 1

        await asyncio.gather(*(one(i) for i in todo))
    print(f"名牌完成，新增 {ok} 张")


def main() -> None:
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    if what in ("all", "covers"):
        asyncio.run(download_covers())
    if what in ("all", "plates"):
        asyncio.run(download_plates())
    if what == "all":
        # 数字渐变素材由另一个脚本生成
        import subprocess

        subprocess.run([sys.executable, str(Path(__file__).with_name("gen_rating_digits.py"))], check=True)


if __name__ == "__main__":
    main()
