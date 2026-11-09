"""从旧 bot 项目提取 otto 活字印刷所需的音频资源。

资源来源：旧项目 ``neko-bot.zip`` 里的 ``data/ottohzys/``。
插件运行时只需要三样东西（约 12MB / 389 个文件）：

- ``tokens/*.wav``       单音节拼音音频，367 个
- ``ysddTokens/*.mp3``   原声大碟整句音频，21 个
- ``ysdd.json``          原声大碟触发短语表

``chinglish.json`` 用的是插件包内 ``res/`` 那份（data 里那份是旧格式，
首字母大写且无空格，与新插件不兼容），这里也一并导出。
旧包的 ``music/``（10MB）和 ``img/`` 完全没被引用，不导出。

用法：

    ./.venv/bin/python tools/extract_otto_assets.py /path/to/neko-bot.zip
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "ottohzys"

SRC_PREFIX = "neko-bot/data/ottohzys/"
CHINGLISH_FROM = "neko-bot/.venv/Lib/site-packages/nonebot_plugin_ottohzys/res/chinglish.json"


def main(zip_path: str) -> None:
    src = Path(zip_path)
    if not src.exists():
        print(f"找不到 {src}")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    tokens, ysdd, others = 0, 0, 0

    with zipfile.ZipFile(src) as z:
        for name in z.namelist():
            if name.startswith("__MACOSX") or "/._" in name:
                continue
            if not name.startswith(SRC_PREFIX):
                continue
            rel = name[len(SRC_PREFIX):]
            if not rel:
                continue
            # 只取三样必需资源
            if rel.startswith("tokens/") and rel.endswith(".wav"):
                target = OUT / rel
                tokens += 1
            elif rel.startswith("ysddTokens/") and rel.endswith(".mp3"):
                target = OUT / rel
                ysdd += 1
            elif rel == "ysdd.json":
                target = OUT / rel
                others += 1
            else:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(name))

        if CHINGLISH_FROM in z.namelist():
            (OUT / "chinglish.json").write_bytes(z.read(CHINGLISH_FROM))
            others += 1

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"导出完成：{tokens} 个音节 wav，{ysdd} 个原声 mp3，{others} 个配置 json")
    print(f"目标目录：{OUT}（{total / 1024 / 1024:.1f} MB）")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1])
