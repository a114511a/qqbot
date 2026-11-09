#!/usr/bin/env python3
"""生成 QQ Bot 系统架构图（SVG + PNG）。

展示各组件之间的连接关系和数据流向：
  用户 QQ → NapCat(Docker) ←反向WS→ NoneBot2 → 各插件 → 外部数据源
"""

from __future__ import annotations

from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# ============================================================
#  统一绘制函数（同时用于 SVG 和 Pillow）
# ============================================================
# 基本图元：rect(圆角矩形), text, arrow(带箭头连线), line

# 调色板
C_BG          = "#ffffff"
C_USER        = "#1a73e8"   # 蓝
C_USER_BG     = "#e8f0fe"
C_NAPCAT      = "#e8710a"   # 橙
C_NAPCAT_BG   = "#fef7e0"
C_NONEBOT     = "#1e8e3e"   # 绿
C_NONEBOT_BG  = "#e6f4ea"
C_PLUGIN      = "#9334e6"   # 紫
C_PLUGIN_BG   = "#f3e8fd"
C_DATA        = "#d93025"   # 红
C_DATA_BG     = "#fce8e6"
C_ASSET       = "#5f6368"   # 灰
C_ASSET_BG    = "#f1f3f4"
C_BORDER      = "#dadce0"
C_ARROW       = "#80868b"
C_TEXT        = "#202124"
C_NOTE        = "#5f6368"
C_ARROW_LABEL = "#3c4043"

# 中文字体候选（按优先级）
FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/STHeiti Light.ttc",
]


def _find_font_file() -> str | None:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


FONT_FILE = _find_font_file()

# 字体
def get_fonts(scale=1):
    if FONT_FILE:
        try:
            return {
                "title":   ImageFont.truetype(FONT_FILE, int(18*scale)),
                "h2":      ImageFont.truetype(FONT_FILE, int(13*scale)),
                "box":     ImageFont.truetype(FONT_FILE, int(12*scale)),
                "box_b":   ImageFont.truetype(FONT_FILE, int(12.5*scale)),
                "small":   ImageFont.truetype(FONT_FILE, int(10*scale)),
                "label":   ImageFont.truetype(FONT_FILE, int(10.5*scale)),
            }
        except OSError:
            pass
    f = ImageFont.load_default()
    return {k: f for k in ["title","h2","box","box_b","small","label"]}


# ============================================================
#  布局描述
# ============================================================
# 画布逻辑尺寸（1x），实际 PNG 用 2x 渲染
W, H = 1100, 720

# 各模块（盒子）: (x, y, w, h, 标题, 副标题列表, 边框色, 背景色)
BOXES = [
    # ── 第一行：用户 ──
    (420, 30, 260, 44, "用户 QQ（手机/电脑客户端）",
     [], C_USER, C_USER_BG),

    # ── 第二行：NapCat ──
    (340, 130, 420, 90, "NapCat  (Docker 容器)",
     ["OneBot V11 协议端 · WebUI 127.0.0.1:6099",
      "端口 3001 · QQ 登录态持久化于 data/qq/"],
     C_NAPCAT, C_NAPCAT_BG),

    # ── 第三行：NoneBot2 ──
    (340, 290, 420, 80, "NoneBot2  (bot.py · 端口 8080)",
     ["反向 WS 路径 /onebot/v11/ws",
      "FastAPI 驱动 · pyproject.toml plugin_dirs 自动发现"],
     C_NONEBOT, C_NONEBOT_BG),

    # ── 第四行：插件层（5 个并排）──
    (30,  430, 190, 110, "group_events",
     ["欢迎新成员", "戳一戳 (CD 10s)"], C_PLUGIN, C_PLUGIN_BG),

    (240, 430, 220, 110, "nonebot_plugin_rikka",
     ["★ 中二/舞萌查分出图", "命令: /chu /mai /bind"], C_PLUGIN, C_PLUGIN_BG),

    (480, 430, 170, 110, "chunithm_bot",
     ["中二底力分析", "命令: /chu plate"], C_PLUGIN, C_PLUGIN_BG),

    (670, 430, 170, 110, "ottohzys",
     ["活字印刷语音合成", "命令: otto..."], C_PLUGIN, C_PLUGIN_BG),

    (860, 430, 210, 110, "mai_arcade",
     ["机厅人数上报/查询", "命令: /机厅人数"], C_PLUGIN, C_PLUGIN_BG),

    # ── 第五行：外部数据源 ──
    (30,  600, 200, 80, "落雪 API (lxns.net)",
     ["中二 B50/R50/玩家资料", "Authorization 无 Bearer"], C_DATA, C_DATA_BG),

    (250, 600, 200, 80, "水鱼 API (diving-fish)",
     ["舞萌 · QQ 号直查", "AP50 受限"], C_DATA, C_DATA_BG),

    (470, 600, 200, 80, "CDN assets2.lxns.net",
     ["曲绘/名牌/段位/角色", "免鉴权直连"], C_ASSET, C_ASSET_BG),

    # ── 右侧：本地存储 ──
    (860, 600, 210, 80, "本地数据",
     ["data/chunithm.sqlite3 绑定", "data/rikka.sqlite3 | static/"], C_ASSET, C_ASSET_BG),
]

# 箭头连线: (x1,y1, x2,y2, 标签, 样式)
# 样式: "down" 垂直向下, "up" 垂直向上, "curve" 曲线, "plain" 直线
ARROWS = [
    # 用户 ↔ NapCat
    (550, 74,  550, 130, "QQ 消息", "down"),
    (490, 130, 490, 74,  "回复消息", "up"),

    # NapCat ↔ NoneBot（反向 WS）
    (500, 220, 500, 290, "反向 WebSocket", "down"),
    (600, 290, 600, 220, "ws://host.docker.internal:8080", "up"),

    # NoneBot → 插件层（分发）
    (550, 370, 550, 430, "事件分发", "down"),

    # ── Rikka → 落雪（从 Rikka 底部最左出发）
    (250, 540, 110, 596, "中二查分", "curve_down"),
    # ── Rikka → 水鱼（从 Rikka 底部偏左）
    (300, 540, 360, 596, "舞萌查分", "curve_down"),
    # ── Rikka → CDN（曲绘）——从 Rikka 底部偏右
    (400, 540, 520, 596, "曲绘下载", "curve_down"),

    # ── chunithm_bot → 落雪（从 chunithm 底部最左端出发，直接走左侧）
    (465, 540, 245, 596, "底力数据", "curve_down"),

    # ── Rikka → 本地存储（绑定/缓存）——从 Rikka 底部右侧远端点
    (450, 540, 880, 596, "绑定/缓存", "curve_down"),

    # ── otto / arcade → 本地存储
    (770, 540, 930, 596, "音频/机厅", "curve_down"),
]


def draw_to_pillow(scale=2):
    """用 Pillow 绘制并保存 PNG。"""
    W2, H2 = W * scale, H * scale
    img = Image.new("RGB", (W2, H2), C_BG)
    d = ImageDraw.Draw(img)
    F = get_fonts(scale)

    def s(v):  # scale helper
        return v * scale

    # 标题
    d.text((s(20), s(8)), "QQ Bot 系统架构图", fill=C_TEXT, font=F["title"])
    d.text((s(20), s(32)), "NoneBot2 + NapCat · OneBot V11 反向 WebSocket", fill=C_NOTE, font=F["small"])

    # ---- 画盒子 ----
    for (x, y, w, h, title, subs, bc, bgc) in BOXES:
        rx, ry, rw, rh = s(x), s(y), s(w), s(h)
        # 背景圆角矩形
        d.rounded_rectangle([rx, ry, rx+rw, ry+rh], radius=s(10), fill=bgc, outline=bc, width=s(2))
        # 标题
        tw = d.textbbox((0,0), title, font=F["box_b"])
        tx = rx + (rw - (tw[2]-tw[0])) // 2
        d.text((tx, ry + s(8)), title, fill=bc, font=F["box_b"])
        # 副标题
        ty = ry + s(28)
        for line in subs:
            d.text((rx + s(10), ty), line, fill=C_NOTE, font=F["small"])
            ty += s(14)

    # ---- 画箭头 ----
    def arrow_down(x1, y1, x2, y2, label):
        """垂直/斜向箭头，带标签。"""
        d.line([(s(x1), s(y1)), (s(x2), s(y2))], fill=C_ARROW, width=s(2))
        # 箭头头部（朝下）
        ah = s(6)
        d.polygon([(s(x2), s(y2)+ah), (s(x2)-ah, s(y2)-ah), (s(x2)+ah, s(y2)-ah)], fill=C_ARROW)
        # 标签
        if label:
            mx = (x1 + x2) // 2
            my = (y1 + y2) // 2
            d.text((s(mx) + s(4), s(my) - s(6)), label, fill=C_ARROW_LABEL, font=F["label"])

    def arrow_up(x1, y1, x2, y2, label):
        d.line([(s(x1), s(y1)), (s(x2), s(y2))], fill=C_ARROW, width=s(2))
        ah = s(6)
        d.polygon([(s(x2), s(y2)-ah), (s(x2)-ah, s(y2)+ah), (s(x2)+ah, s(y2)+ah)], fill=C_ARROW)
        if label:
            mx = (x1 + x2) // 2
            my = (y1 + y2) // 2
            d.text((s(mx) + s(4), s(my) - s(6)), label, fill=C_ARROW_LABEL, font=F["label"])

    def curve_down(x1, y1, x2, y2, label):
        """平滑斜向连线：从 (x1,y1) 到 (x2,y2)，先垂直下降一段，再斜向直线到达。

        标签放在斜线段中点，用白色底衬避免与线重叠，多个标签自然错开。
        """
        drop = min(28, (y2 - y1) // 2)  # 从起点向下垂落的长度
        mid_y = y1 + drop
        # 垂直下降段
        d.line([(s(x1), s(y1)), (s(x1), s(mid_y))], fill=C_ARROW, width=s(2))
        # 斜线段
        d.line([(s(x1), s(mid_y)), (s(x2), s(y2))], fill=C_ARROW, width=s(2))
        # 箭头头部（朝下）
        ah = s(6)
        d.polygon([(s(x2), s(y2)+ah), (s(x2)-ah, s(y2)-ah), (s(x2)+ah, s(y2)-ah)], fill=C_ARROW)
        # 标签放在斜线段靠起点 35% 处，带白色底衬，避免多条长线标签相撞
        if label:
            t = 0.35
            lx = int(x1 + (x2 - x1) * t)
            ly = int(mid_y + (y2 - mid_y) * t)
            bbox = d.textbbox((0, 0), label, font=F["label"])
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            pad = s(3)
            d.rectangle(
                [s(lx) - tw // 2 - pad, s(ly) - th // 2 - pad,
                 s(lx) + tw // 2 + pad, s(ly) + th // 2 + pad],
                fill=C_BG,
            )
            d.text((s(lx) - tw // 2, s(ly) - th // 2), label, fill=C_ARROW_LABEL, font=F["label"])

    for (x1, y1, x2, y2, label, style) in ARROWS:
        if style == "down":
            arrow_down(x1, y1, x2, y2, label)
        elif style == "up":
            arrow_up(x1, y1, x2, y2, label)
        elif style == "curve_down":
            curve_down(x1, y1, x2, y2, label)

    # ---- 底部图例 ----
    ly = s(700)
    legend = [
        (C_USER,    "用户"),
        (C_NAPCAT,  "协议端"),
        (C_NONEBOT, "Bot 框架"),
        (C_PLUGIN,  "插件"),
        (C_DATA,    "外部 API"),
        (C_ASSET,   "本地/CDN"),
    ]
    lx = s(20)
    for color, text in legend:
        d.rounded_rectangle([lx, ly, lx+s(14), ly+s(14)], radius=s(3), fill=color)
        d.text((lx + s(20), ly - s(1)), text, fill=C_TEXT, font=F["small"])
        lx += s(20) + d.textbbox((0,0), text, font=F["small"])[2] + s(16)

    png_path = OUT_DIR / "项目架构图.png"
    img.save(png_path, "PNG")
    print(f"PNG 已生成: {png_path}")
    return png_path


def draw_to_svg():
    """用 SVG 绘制并保存。"""
    parts = []
    parts.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">')
    parts.append(f'<rect width="{W}" height="{H}" fill="{C_BG}"/>')
    FONT = "font-family: -apple-system, 'PingFang SC', sans-serif;"

    # 标题
    parts.append(f'<text x="20" y="26" {FONT} font-size="18" font-weight="700" fill="{C_TEXT}">QQ Bot 系统架构图</text>')
    parts.append(f'<text x="20" y="44" {FONT} font-size="10" fill="{C_NOTE}">NoneBot2 + NapCat · OneBot V11 反向 WebSocket</text>')

    # defs: 箭头标记
    parts.append('<defs>')
    parts.append(f'<marker id="arrowhead" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto"><polygon points="0,0 8,4 0,8" fill="{C_ARROW}"/></marker>')
    parts.append('</defs>')

    # 盒子
    for (x, y, w, h, title, subs, bc, bgc) in BOXES:
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" ry="10" fill="{bgc}" stroke="{bc}" stroke-width="2"/>')
        # 标题居中
        parts.append(f'<text x="{x+w/2}" y="{y+22}" {FONT} font-size="12.5" font-weight="600" fill="{bc}" text-anchor="middle">{title}</text>')
        ty = y + 38
        for line in subs:
            parts.append(f'<text x="{x+10}" y="{ty}" {FONT} font-size="10" fill="{C_NOTE}">{line}</text>')
            ty += 14

    # 箭头
    for (x1, y1, x2, y2, label, style) in ARROWS:
        if style in ("down", "up"):
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{C_ARROW}" stroke-width="2" marker-end="url(#arrowhead)"/>')
            mx, my = (x1+x2)/2, (y1+y2)/2
            parts.append(f'<text x="{mx+4}" y="{my-4}" {FONT} font-size="10.5" fill="{C_ARROW_LABEL}">{label}</text>')
        elif style == "curve_down":
            drop = min(28, (y2 - y1) // 2)
            mid_y = y1 + drop
            path = f'M {x1} {y1} L {x1} {mid_y} L {x2} {y2}'
            parts.append(f'<path d="{path}" fill="none" stroke="{C_ARROW}" stroke-width="2" marker-end="url(#arrowhead)"/>')
            parts.append(f'<text x="{x1+5}" y="{y1+drop//2}" {FONT} font-size="10.5" fill="{C_ARROW_LABEL}">{label}</text>')

    # 图例
    ly = 700
    lx = 20
    legend = [
        (C_USER, "用户"), (C_NAPCAT, "协议端"), (C_NONEBOT, "Bot 框架"),
        (C_PLUGIN, "插件"), (C_DATA, "外部 API"), (C_ASSET, "本地/CDN"),
    ]
    for color, text in legend:
        parts.append(f'<rect x="{lx}" y="{ly}" width="14" height="14" rx="3" fill="{color}"/>')
        parts.append(f'<text x="{lx+20}" y="{ly+11}" {FONT} font-size="10" fill="{C_TEXT}">{text}</text>')
        lx += 20 + len(text) * 8 + 16

    parts.append('</svg>')
    svg_path = OUT_DIR / "项目架构图.svg"
    svg_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"SVG 已生成: {svg_path}")
    return svg_path


if __name__ == "__main__":
    draw_to_svg()
    if HAS_PIL:
        draw_to_pillow()
    else:
        print("Pillow 未安装，跳过 PNG（SVG 已可用）")
