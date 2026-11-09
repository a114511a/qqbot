"""下载舞萌 icon / plate / frame 静态资源（来自 assets2.lxns.net）。

幂等：已存在且是合法图片的跳过；缺的直接补。
用法：
    ./.venv/bin/python tools/download_mai_assets.py [--force]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

for _k in ("NO_PROXY", "no_proxy"):
    import os

    os.environ[_k] = ",".join(
        p.replace("[", "").replace("]", "")
        for p in os.environ.get(_k, "").split(",")
        if p.strip()
    ) or "127.0.0.1,localhost"

import httpx
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAI_DIR = PROJECT_ROOT / "static" / "mai"
BASE = "https://assets2.lxns.net/maimai"

# 各类素材的 ID 扫描上限（实测能下到的最大 ID 再加一点余量）
TARGETS = {
    "icon": 420,
    "plate": 400,
    "frame": 20,
}


def _is_valid_image(path: Path) -> bool:
    if not path.exists() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


async def _download_one(
    client: httpx.AsyncClient, kind: str, idx: int, out_dir: Path, force: bool, sem: asyncio.Semaphore
) -> tuple[str, int]:
    """返回 (状态, 字节数)。状态为 ok / skip / missing / bad。"""
    out = out_dir / f"{idx}.png"
    if not force and _is_valid_image(out):
        return "skip", out.stat().st_size

    async with sem:
        try:
            r = await client.get(f"{BASE}/{kind}/{idx}.png")
        except Exception:
            return "missing", 0

    if r.status_code != 200 or not r.content:
        return "missing", 0

    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(r.content)
    if not _is_valid_image(tmp):
        tmp.unlink(missing_ok=True)
        return "bad", 0
    tmp.replace(out)
    return "ok", len(r.content)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="重新下载已有文件")
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()

    sem = asyncio.Semaphore(args.workers)
    total = {"ok": 0, "skip": 0, "missing": 0, "bad": 0}
    saved = 0

    async with httpx.AsyncClient(
        trust_env=False, timeout=30, follow_redirects=True
    ) as client:
        for kind, limit in TARGETS.items():
            out_dir = MAI_DIR / kind
            out_dir.mkdir(parents=True, exist_ok=True)
            tasks = [
                _download_one(client, kind, i, out_dir, args.force, sem)
                for i in range(1, limit + 1)
            ]
            results = await asyncio.gather(*tasks)
            stat = {"ok": 0, "skip": 0, "missing": 0, "bad": 0}
            size = 0
            for status, n in results:
                stat[status] += 1
                size += n
            for k, v in stat.items():
                total[k] += v
            saved += size
            print(
                f"{kind:6s} 新增 {stat['ok']:3d}  跳过 {stat['skip']:3d}  "
                f"无此项 {stat['missing']:3d}  损坏 {stat['bad']:2d}  "
                f"({size / 1024:.0f} KB)"
            )

    print(f"\n合计新增 {total['ok']} 个文件，{saved / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
