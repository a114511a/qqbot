"""otto 活字印刷 · 音频合成核心。

算法源自 MIT 许可的 nonebot-plugin-ottohzys 0.2.1（作者 student_2333 / lgc-NB2Dev），
本项目做了以下适配：

- 去掉 GPLv3 的 ``psola`` / ``praat-parselmouth``：变速变调用 numpy 线性
  重采样近似，避免引入 macOS 上难装的原生轮子与许可证负担。
- 资源路径改为相对本插件的项目根目录，不再依赖 ``Path.cwd()``。
- 输出除 WAV 外可经 ffmpeg 转 MP3，兼容 NapCat 的语音段。
- 修掉原版 ``-N/--no-normalize`` 默认 True 导致归一化永不生效的 bug。

资源（``data/ottohzys/``）来自旧 bot 项目，只保留必需部分：
tokens/*.wav（367）、ysddTokens/*.mp3（21）、ysdd.json、chinglish.json。
"""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import soundfile as sf
from nonebot import logger
from pypinyin import lazy_pinyin

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESOURCE_DIR = PROJECT_ROOT / "data" / "ottohzys"
TOKENS_DIR = RESOURCE_DIR / "tokens"
YSDD_TOKENS_DIR = RESOURCE_DIR / "ysddTokens"

TARGET_SAMPLE_RATE = 44100


@lru_cache(maxsize=1)
def _chinglish_map() -> dict[str, str]:
    p = RESOURCE_DIR / "chinglish.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@lru_cache(maxsize=1)
def _ysdd_token_map() -> dict[str, list[str]]:
    p = RESOURCE_DIR / "ysdd.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _normalize(data: np.ndarray) -> np.ndarray:
    rms = float(np.sqrt(np.mean(data**2)))
    return data if rms == 0 else data / rms * 0.2


@lru_cache(maxsize=512)
def _load_audio(path_str: str, normalize: bool = True) -> np.ndarray:
    p = Path(path_str)
    data, rate = sf.read(str(p), dtype="float64")
    if len(data.shape) == 2:
        data = (data[:, 0] + data[:, 1]) / 2
    if rate != TARGET_SAMPLE_RATE:
        new_len = max(1, int(TARGET_SAMPLE_RATE / rate * len(data)))
        data = np.interp(
            np.arange(new_len),
            np.linspace(0, new_len - 1, len(data)),
            data,
        )
    return _normalize(data) if normalize else data


@lru_cache(maxsize=512)
def _get_pinyin_audio(
    pinyin: str, is_ysdd: bool = False, normalize: bool = True, empty_length: float = 0.25
) -> np.ndarray:
    if pinyin != "_":
        base = YSDD_TOKENS_DIR if is_ysdd else TOKENS_DIR
        path = base / f"{pinyin}{'.mp3' if is_ysdd else '.wav'}"
        if path.exists():
            try:
                return _load_audio(str(path), normalize)
            except Exception as e:
                logger.warning(f"[otto] 读取音频失败 {path.name}: {e}")
        else:
            logger.debug(f"[otto] 缺少音节 {path.name}，用静音代替")
    return np.zeros(int(empty_length * TARGET_SAMPLE_RATE))


@dataclass
class _Pron:
    pron: str
    is_pinyin: bool = False
    is_ysdd: bool = False


def _parse_sentence(sentence: str, ysdd_mode: bool = True) -> list[_Pron]:
    units: list[_Pron] = [_Pron(sentence)]

    def replace(kw: str, *replacements: _Pron) -> list[_Pron]:
        tmp: list[_Pron] = []
        for unit in units:
            if kw not in unit.pron or unit.is_pinyin or unit.is_ysdd:
                tmp.append(unit)
                continue
            parts = unit.pron.split(kw)
            for n, seg in enumerate(parts):
                if n:
                    tmp.extend(replacements)
                if seg:
                    tmp.append(_Pron(seg))
        return tmp

    if ysdd_mode:
        for token, phrases in _ysdd_token_map().items():
            for phrase in phrases:
                units = replace(phrase, _Pron(token, is_ysdd=True))

    for ch, pron in _chinglish_map().items():
        units = replace(ch, *(_Pron(p, is_pinyin=True) for p in pron.split()))

    ret: list[_Pron] = []
    for unit in units:
        if unit.is_ysdd or unit.is_pinyin:
            ret.append(unit)
            continue
        for p in lazy_pinyin(unit.pron):
            if p.isalpha():
                ret.append(_Pron(p, is_pinyin=True))
            else:
                ret.extend(_Pron("_", is_pinyin=True) for _ in p)
    return ret


def _modify_pitch_speed(data: np.ndarray, pitch: float, speed: float) -> np.ndarray:
    """不用 PSOLA 的近似变速变调：整体线性重采样。

    speed>1 变快且音调升高，pitch 单独作用时再做一次补偿重采样。
    与原版 PSOLA 效果不完全一致，但对「鬼叫」类玩法足够。
    """
    if pitch == 1 and speed == 1:
        return data
    factor = speed * pitch
    if factor <= 0:
        return data
    new_len = max(1, int(len(data) / factor))
    return np.interp(
        np.arange(new_len),
        np.linspace(0, new_len - 1, len(data)),
        data,
    )


def generate_wav(
    sentence: str,
    reverse: bool = False,
    ysdd_mode: bool = True,
    normalize: bool = True,
    speed: float = 1.0,
    pitch: float = 1.0,
    pause: float = 0.25,
) -> io.BytesIO:
    """合成语音，返回 44100Hz PCM_16 WAV 的 BytesIO。"""
    units = _parse_sentence(sentence.lower(), ysdd_mode)
    audios = [
        _get_pinyin_audio(u.pron, u.is_ysdd, normalize, pause) for u in units
    ]
    if not audios:
        audios = [np.zeros(int(pause * TARGET_SAMPLE_RATE))]
    data = np.concatenate(audios)
    data = _modify_pitch_speed(data, pitch, speed)
    if reverse:
        data = np.flip(data)
    buf = io.BytesIO()
    sf.write(buf, data, TARGET_SAMPLE_RATE, format="wav", subtype="PCM_16")
    buf.seek(0)
    return buf


def wav_to_mp3(wav: io.BytesIO) -> io.BytesIO:
    """WAV → MP3。QQ 语音对 mp3 兼容性更好；ffmpeg 不可用时原样返回 WAV。"""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0",
             "-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3", "pipe:1"],
            input=wav.getvalue(),
            capture_output=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout:
            return io.BytesIO(proc.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(f"[otto] ffmpeg 转码失败，回退 WAV: {e}")
    wav.seek(0)
    return wav


async def generate_voice_bytes(sentence: str, **kwargs) -> io.BytesIO:
    """异步包装：合成在线程池里跑，避免阻塞事件循环。"""
    wav = await asyncio.to_thread(generate_wav, sentence, **kwargs)
    return await asyncio.to_thread(wav_to_mp3, wav)
