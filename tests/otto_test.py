"""otto 活字印刷功能测试。

覆盖：
  1. 语音素材完整性（tokens / ysddTokens / 映射表）
  2. 文本解析 _parse_sentence（ysdd 整句替换、chinglish 字母、拼音拆分）
  3. 音频合成 generate_wav / generate_voice_bytes（非静音、MP3 输出）
  4. 参数：倒放、变速、变调、不归一化
  5. 发送格式：base64:// record 段（NapCat 在 Docker 内无法读宿主机 file://）
  6. 原版已知 bug 回归（-N 默认值、=N 解析等由插件其余部分保证）

运行：
    ./.venv/bin/python tests/otto_test.py
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src" / "plugins"))

spec = importlib.util.spec_from_file_location(
    "otto_audio_under_test", PROJECT_ROOT / "src" / "plugins" / "ottohzys" / "audio.py"
)
m = importlib.util.module_from_spec(spec)
sys.modules["otto_audio_under_test"] = m
spec.loader.exec_module(m)

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
# 1. 素材完整性
# ---------------------------------------------------------------------------
TOKENS = m.TOKENS_DIR
YSDD = m.YSDD_TOKENS_DIR
chinglish = json.loads((m.RESOURCE_DIR / "chinglish.json").read_text(encoding="utf-8"))
ysdd = json.loads((m.RESOURCE_DIR / "ysdd.json").read_text(encoding="utf-8"))

n_wav = len(list(TOKENS.glob("*.wav")))
n_mp3 = len(list(YSDD.glob("*.mp3")))
check_true(f"tokens 音节音频存在（{n_wav} 个）", n_wav > 300)
check_true(f"ysddTokens 原声存在（{n_mp3} 个）", n_mp3 > 10)
check("chinglish 映射条目", len(chinglish), 39)
check("ysdd 映射条目", len(ysdd), 21)

missing_ysdd = [k for k in ysdd if not (YSDD / f"{k}.mp3").exists()]
check("ysdd.json 里的原声都有 mp3", missing_ysdd, [])

# 这些音节原项目本身就没有，属已知缺口，不算迁移遗漏；解析时会用静音代替，
# 但用户常用句「大家好啊」被 ysdd 整句替换，不受影响。
KNOWN_MISSING = {"ce", "kei"}
check("已知缺口与原项目一致", KNOWN_MISSING - {p.stem for p in TOKENS.glob("*.wav")}, KNOWN_MISSING)

# 全部音节文件都能被 soundfile 读出来（抽查前 30 个 + ysdd 全部）
bad = []
for p in list(TOKENS.glob("*.wav"))[:30]:
    try:
        sf.read(str(p), dtype="float64")
    except Exception:
        bad.append(p.name)
check("抽查 30 个 wav 均可解码", bad, [])
bad_mp3 = []
for p in YSDD.glob("*.mp3"):
    try:
        sf.read(str(p), dtype="float64")
    except Exception:
        bad_mp3.append(p.name)
check("全部 ysdd mp3 均可解码", bad_mp3, [])

# ---------------------------------------------------------------------------
# 2. 文本解析
# ---------------------------------------------------------------------------
units = m._parse_sentence("大家好啊", ysdd_mode=True)
check("ysdd 整句替换成 djha", [u.pron for u in units], ["djha"])
check_true("ysdd 单元标记 is_ysdd", units[0].is_ysdd)

units = m._parse_sentence("abc", ysdd_mode=True)
prons = [u.pron for u in units]
check("chinglish 字母替换", prons, ["ei", "bi", "xi"])

units = m._parse_sentence("你好", ysdd_mode=False)
check_true("中文拆成拼音", len(units) == 2 and all(u.is_pinyin for u in units))

# ---------------------------------------------------------------------------
# 3. 合成：非静音 + MP3
# ---------------------------------------------------------------------------
async def synth_test():
    cases = [
        ("大家好啊 我阐述你的梦", {}),
        ("测试", {}),
        ("otto活字印刷", {}),
        ("大家好啊", {"reverse": True}),
        ("大家好啊", {"speed": 1.5}),
        ("大家好啊", {"pitch": 1.3}),
        ("大家好啊", {"normalize": False}),
        ("大家好啊", {"ysdd_mode": False}),
    ]
    for sentence, kw in cases:
        bio = await m.generate_voice_bytes(sentence, **kw)
        raw = bio.getvalue()
        label = f"{sentence!r} {kw or ''}".strip()
        check_true(f"{label}: 有输出（{len(raw)} bytes）", len(raw) > 3000)
        is_mp3 = raw[:3] == b"ID3" or raw[:2] == b"\xff\xfb"
        check_true(f"{label}: 输出为 MP3", is_mp3)
        bio.seek(0)
        data, sr = sf.read(bio)
        rms = float(np.sqrt(np.mean(data ** 2)))
        check_true(f"{label}: 非静音 (rms={rms:.3f})", rms > 0.01)
        check(f"{label}: 采样率 44100", sr, 44100)


asyncio.run(synth_test())

# ---------------------------------------------------------------------------
# 4. 发送格式：base64://（关键回归项）
# ---------------------------------------------------------------------------
src = (PROJECT_ROOT / "src" / "plugins" / "ottohzys" / "__init__.py").read_text(encoding="utf-8")
check_true('源码使用 base64:// 发送语音', 'base64://' in src)
check_true('源码不再使用 file:// 绝对路径', 'as_uri()' not in src)

voice = asyncio.run(m.generate_voice_bytes("大家好啊"))
raw = voice.getvalue()
b64 = base64.b64encode(raw).decode()
seg_file = f"base64://{b64}"
check_true("base64 段可还原回原始字节", base64.b64decode(seg_file[len("base64://"):]) == raw)

print(f"\n共 {PASS + FAIL} 项检查，PASS {PASS}，FAIL {FAIL}")
sys.exit(1 if FAIL else 0)
