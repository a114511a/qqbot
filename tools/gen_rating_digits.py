"""生成 RATING 数字用的「彩」「彩极」渐变素材。

LXNS 只提供了 rainbow（黄绿青）一套数字素材，但玩家 Rating 到 17 之后
游戏里显示的是「彩极」配色（黄→橙红→品红→紫→蓝→青）。这里以现有
``*_rainbow.png`` 的形状和黑色描边为模板，只替换填充色，生成两套素材：

- ``*_rainbow1.png`` / ``rating_rainbow1.png`` / ``point_rainbow1.png``
  16.00+ 的「彩」：黄 → 黄绿 → 绿 → 青绿 → 青
- ``*_rainbow2.png`` / ``rating_rainbow2.png`` / ``point_rainbow2.png``
  17.00+ 的「彩极」：黄 → 橙红 → 品红 → 紫 → 蓝 → 青

色标取自游戏内 RATING 显示截图。重新生成：

    ./.venv/bin/python tools/gen_rating_digits.py
"""

from pathlib import Path

import numpy as np
from PIL import Image

PIC_DIR = Path(__file__).resolve().parents[1] / "static" / "chu" / "pic"

# 自上而下的渐变色标
STOPS_RAINBOW1 = [  # 16.00+ 彩
    (255, 200, 100),
    (245, 235, 110),
    (200, 240, 90),
    (110, 235, 120),
    (80, 225, 200),
    (70, 205, 235),
]
STOPS_RAINBOW2 = [  # 17.00+ 彩极
    (255, 224, 90),
    (250, 120, 70),
    (230, 70, 150),
    (150, 60, 210),
    (80, 140, 230),
    (70, 220, 215),
]


def gradient(height: int, stops: list[tuple[int, int, int]]) -> np.ndarray:
    """按高度生成垂直渐变，返回 (height, 3) 的 float 数组。"""
    out = np.zeros((height, 3), dtype=float)
    n = len(stops) - 1
    for y in range(height):
        t = y / max(1, height - 1) * n
        k = min(int(t), n - 1)
        f = t - k
        out[y] = np.array(stops[k], float) * (1 - f) + np.array(stops[k + 1], float) * f
    return out


def recolor(src: Path, dst: Path, stops: list[tuple[int, int, int]]) -> None:
    """保留原图的 alpha 与黑色描边，把填充部分换成垂直渐变。"""
    im = Image.open(src).convert("RGBA")
    arr = np.array(im)
    alpha = arr[:, :, 3]
    rgb = arr[:, :, :3].astype(float)
    ink = rgb.mean(2) < 70  # 黑色描边
    height = alpha.shape[0]
    grad = gradient(height, stops)
    for y in range(height):
        rgb[y, :, :] = np.where(ink[y, :, None], rgb[y, :, :], grad[y][None, :])
    Image.fromarray(np.dstack([rgb.astype(np.uint8), alpha]), "RGBA").save(dst)


def main() -> None:
    templates = sorted(PIC_DIR.glob("*_rainbow.png")) + [
        PIC_DIR / "rating_rainbow.png",
        PIC_DIR / "point_rainbow.png",
    ]
    for variant, stops in (("rainbow1", STOPS_RAINBOW1), ("rainbow2", STOPS_RAINBOW2)):
        count = 0
        for src in templates:
            if not src.exists():
                continue
            dst = PIC_DIR / src.name.replace("_rainbow.png", f"_{variant}.png")
            recolor(src, dst, stops)
            count += 1
        print(f"生成 {variant}: {count} 个文件 -> {PIC_DIR}")


if __name__ == "__main__":
    main()
