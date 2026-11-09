"""从旧 bot 项目（neko-bot）迁移 4 个功能的相关数据。

只做数据迁移，不改任何项目代码。迁移的内容：

1. **otto 活字印刷音频** → ``data/ottohzys/``
   - ``tokens/*.wav``（367 个，约 9.6MB）
   - ``ysddTokens/*.mp3``（21 个，约 1.1MB）
   - ``ysdd.json`` / ``chinglish.json``
   - 旧包的 ``music/``（10MB）与 ``img/`` 未被插件引用，不迁移

2. **群功能开关** → 打印成 .env 片段
   - 来自 ``data/eventmonitor/config.json`` 的 64 个群号
   - 生成 ``WELCOME_GROUPS`` / ``POKE_GROUPS``

不迁移（旧包里本来就没有，或与本项目无关）：
- 机厅人数：原版数据存在 Windows AppData，zip 内不存在 ``arcade_data.json``
- ``data/bottle``（1GB 漂流瓶缓存）、``data/impact``、``data/setu4`` 等与本次功能无关

⚠️ 迁移出来的数据**不进 Git**：``data/`` 与 ``.env`` 都已在 .gitignore 中。

用法：

    ./.venv/bin/python tools/migrate_from_neko_bot.py /path/to/neko-bot.zip
    ./.venv/bin/python tools/migrate_from_neko_bot.py /path/to/neko-bot.zip --groups-only
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OTTO_OUT = ROOT / "data" / "ottohzys"

OTTO_PREFIX = "neko-bot/data/ottohzys/"
CHINGLISH_FROM = "neko-bot/.venv/Lib/site-packages/nonebot_plugin_ottohzys/res/chinglish.json"
EVENTMONITOR_CFG = "neko-bot/data/eventmonitor/config.json"


def migrate_otto(z: zipfile.ZipFile) -> None:
    OTTO_OUT.mkdir(parents=True, exist_ok=True)
    tokens = ysdd = others = 0
    for name in z.namelist():
        if name.startswith("__MACOSX") or "/._" in name:
            continue
        if not name.startswith(OTTO_PREFIX):
            continue
        rel = name[len(OTTO_PREFIX):]
        if not rel:
            continue
        if rel.startswith("tokens/") and rel.endswith(".wav"):
            target, tokens = OTTO_OUT / rel, tokens + 1
        elif rel.startswith("ysddTokens/") and rel.endswith(".mp3"):
            target, ysdd = OTTO_OUT / rel, ysdd + 1
        elif rel == "ysdd.json":
            target, others = OTTO_OUT / rel, others + 1
        else:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(z.read(name))

    if CHINGLISH_FROM in z.namelist():
        (OTTO_OUT / "chinglish.json").write_bytes(z.read(CHINGLISH_FROM))
        others += 1

    total = sum(p.stat().st_size for p in OTTO_OUT.rglob("*") if p.is_file())
    print(f"[otto] 迁移 {tokens} 个音节 wav、{ysdd} 个原声 mp3、{others} 个 json "
          f"→ {OTTO_OUT}（{total / 1024 / 1024:.1f} MB）")


def migrate_groups(z: zipfile.ZipFile) -> None:
    if EVENTMONITOR_CFG not in z.namelist():
        print("[群配置] 未找到 eventmonitor/config.json，跳过")
        return
    cfg = json.loads(z.read(EVENTMONITOR_CFG).decode("utf-8"))
    welcome = [g for g, v in cfg.items() if v.get("add_user")]
    poke = [g for g, v in cfg.items() if v.get("chuo")]
    print(f"[群配置] 共 {len(cfg)} 个群；开启欢迎 {len(welcome)} 个，开启戳一戳 {len(poke)} 个")
    print("\n把下面几行加到 .env（已在 .gitignore 中，不会进 Git）：\n")
    print(f"WELCOME_ENABLED=true")
    print(f"WELCOME_GROUPS={json.dumps(welcome, ensure_ascii=False)}")
    print(f"POKE_ENABLED=true")
    print(f"POKE_GROUPS={json.dumps(poke, ensure_ascii=False)}")
    print(f"POKE_CD=2")
    print(f"ARCADE_GROUPS=[]\n")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    src = Path(args[0])
    if not src.exists():
        print(f"找不到 {src}")
        sys.exit(1)

    with zipfile.ZipFile(src) as z:
        if "--groups-only" not in sys.argv:
            migrate_otto(z)
        migrate_groups(z)

    print("完成。机厅人数在本项目里没有历史数据需要迁移（原版存在 Windows AppData）。")


if __name__ == "__main__":
    main()
