import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

import numpy as np

from nonebot import logger
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
from PIL.Image import Resampling

from ...config import config
from ...database.crud import ChuSongORM
from ...score import PlayerChuInfo, PlayerChuScore
from ._config import (
    ACH_TO_IMAGE,
    B30_BG,
    CLASS_DIR,
    COVER_DIR,
    DIFF_COLORS,
    DIFF_TO_FRAME,
    FONT_MAIN,
    FONT_NUM,
    FONT_TITLE,
    ICON_DIR,
    PIC_DIR,
    PLATE_DIR,
    RANK_TO_IMAGE,
    TROPHY_COLOR_INDEX,
    get_rating_color_name,
    score_to_rank,
)

GAP_X = 8  # horizontal gap between cards
GAP_Y = 8  # vertical gap between card rows


class ScoreBaseImage:
    _draw: ImageDraw.ImageDraw

    def __init__(self, image: Optional[Image.Image] = None) -> None:
        """
        初始化 ScoreBaseImage

        :param image: 可选的 PIL Image 对象，如果提供则在其上绘图
        """
        self._bg = image

        self.width = 1400
        self.height = 1800

        self.header_h = 240
        self.section_gap = 30
        # 两侧留白参考社区成绩图：约占画布宽度 1.5%
        self.section_pad_x = 22
        self.section_height = 44
        self.footer_h = 42

        self._col = 5
        self._card_width = (self.width - 2 * self.section_pad_x - (self._col - 1) * GAP_X) // self._col
        # 卡片高度与左侧曲绘同高：曲绘满高铺在卡片左边，白框上下沿与曲绘对齐。
        # 宽高比约 3:1（264×88），比原来 1.95:1 更扁，贴合参考图的横条卡片。
        self._card_height = int(self._card_width * 0.335)

        self._reset_im()

    @staticmethod
    @lru_cache(64)
    def _f(path: Path, size: int) -> ImageFont.FreeTypeFont:
        """
        使用 lru 缓存加载字体
        """
        return ImageFont.truetype(str(path), size, encoding="utf-8")

    @staticmethod
    @lru_cache(64)
    def _load_resources(path: Path) -> Image.Image:
        """
        使用 lru 缓存加载本地资源
        """
        return Image.open(path).convert("RGBA")

    @staticmethod
    def _trunc(text: str, max_px: int, font, draw) -> str:
        while text and draw.textlength(text, font=font) > max_px:
            text = text[:-1]
        return text

    @staticmethod
    def _wrap_text(text: str, max_px: int, font, draw, max_lines: int = 2) -> list[str]:
        """按像素宽度折行，最多 max_lines 行；超出部分用省略号收尾。

        曲名有英文也有日文，英文按单词断行、日文/中文按字符断行，
        避免出现 "JIGOKU STATION C" 这种被硬切的情况。
        """
        if draw.textlength(text, font=font) <= max_px:
            return [text]

        lines: list[str] = []
        rest = text
        while rest and len(lines) < max_lines:
            # 最后一行还有剩余内容时，直接截断加省略号
            if len(lines) == max_lines - 1 and draw.textlength(rest, font=font) > max_px:
                lines.append(ScoreBaseImage._trunc(rest, max_px, font, draw)[:-1] + "…")
                return lines

            # 二分找出这一行能放下的最长前缀
            lo, hi = 1, len(rest)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if draw.textlength(rest[:mid], font=font) <= max_px:
                    lo = mid
                else:
                    hi = mid - 1
            cut = lo
            # 英文尽量在空格处断开
            if cut < len(rest) and rest[cut] != " " and " " in rest[:cut]:
                space = rest.rfind(" ", 0, cut)
                if space > cut // 2:
                    cut = space
            lines.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()

        if rest:
            lines[-1] = lines[-1][:-1] + "…"
        return lines

    @staticmethod
    def _score_str(s: int) -> str:
        return f"{s:,}"

    @staticmethod
    def _achievement_badge(score: PlayerChuScore) -> Optional[Image.Image]:
        """
        返回 fc 或 clear 微标
        """
        ach_badge: Optional[str] = None
        if score.full_combo:
            ach_badge = ACH_TO_IMAGE.get(score.full_combo.value, "fullcombo")
        # elif score.full_chain:
        #     ach_badge = ACH_TO_IMAGE.get(score.full_chain.value, "fullchain")
        elif score.clear:
            ach_badge = "clear.png"
        if ach_badge:
            badge_path = PIC_DIR / ach_badge
            return ScoreBaseImage._load_resources(badge_path)
        return None

    @staticmethod
    def _score_badge(score: PlayerChuScore) -> Image.Image:
        """
        返回评级微标
        """
        rank_str = score_to_rank(score.score)
        rank_idx = RANK_TO_IMAGE.get(rank_str, 0)
        rank_path = PIC_DIR / f"icon_rank_{rank_idx}.png"
        return ScoreBaseImage._load_resources(rank_path)

    @staticmethod
    def _get_song_level_value(score: PlayerChuScore) -> str:
        """取谱面具体定数（如 ``14.9``）。

        曲目定数存在 ORM 缓存里，取不到时退回 API 给的标级（如 ``14+``），
        并打一条 warning 方便排查缓存是否没刷新。
        注意 ``level_value`` 是 float，整数定数要显示成 ``13`` 而不是 ``13.0``。
        """
        song_id = score.song_id
        song = ChuSongORM.get_song_sync(song_id)
        if not song:
            logger.warning(f"曲目 {song_id} 不存在乐曲对象！")
            return score.song_level
        diff = int(score.song_difficulty.value)
        try:
            level_value = float(song.difficulties.difficulties[diff].level_value)
        except (IndexError, AttributeError, TypeError, ValueError):
            return score.song_level
        # 14.9 → "14.9"；14.0 → "14"；13.25 → "13.25"
        return f"{level_value:g}"

    def _make_bg(self) -> Image.Image:
        """生成背景。

        优先使用 static/chu/bg/b30_bg.png（浅色插画背景，参考社区成绩图风格）；
        没有该文件时回退到内置的深色点阵背景。

        背景只做装饰，但插画本身噪点多，直接进 PNG 会让整张图膨胀到 2.4MB 左右。
        体积主要由 image_to_bytes 的量化压缩兜底，这里只做一次轻微柔化，
        进一步降低噪点，视觉上看不出差别。
        """
        if B30_BG.exists():
            bg = Image.open(B30_BG).convert("RGBA")
            # 按 cover 方式等比缩放铺满画布，避免拉伸变形
            scale = max(self.width / bg.width, self.height / bg.height)
            new_size = (max(1, int(bg.width * scale)), max(1, int(bg.height * scale)))
            bg = bg.resize(new_size, Resampling.LANCZOS)
            left = (bg.width - self.width) // 2
            top = (bg.height - self.height) // 2
            bg = bg.crop((left, top, left + self.width, top + self.height))
            # 轻微降采样 + 柔化，压掉插画的高频噪点（文字是后画的，不受影响）
            small = bg.resize((max(1, self.width // 2), max(1, self.height // 2)), Resampling.LANCZOS)
            bg = small.filter(ImageFilter.GaussianBlur(1.0)).resize(
                (self.width, self.height), Resampling.LANCZOS
            )
            # 盖一层半透明白，压淡插画让上面的文字/卡片更清晰
            veil = Image.new("RGBA", (self.width, self.height), (255, 255, 255, 88))
            return Image.alpha_composite(bg, veil)

        img = Image.new("RGBA", (self.width, self.height), (16, 12, 28, 255))
        draw = ImageDraw.Draw(img)
        # Dot grid
        for gx in range(0, self.width, 30):
            for gy in range(0, self.height, 30):
                draw.ellipse((gx, gy, gx + 1, gy + 1), fill=(255, 255, 255, 16))
        # Diagonal lines top-right
        for i in range(0, 240, 24):
            draw.line([(self.width - 320 + i, 0), (self.width, 320 - i)], fill=(255, 255, 255, 8), width=1)
        return img

    def _reset_im(self):
        """重置当前图像内容为纯背景图"""
        if self._bg:
            self._im = self._bg
        elif config.scorelist_bg:
            self._im = Image.open(config.scorelist_bg).convert("RGBA").resize((self.width, self.height))
        else:
            self._im = self._make_bg()

        self._draw = ImageDraw.Draw(self._im)

    @staticmethod
    @lru_cache(maxsize=1)
    def _plate_has_content(path_str: str) -> bool:
        """判断底板图是否有实际内容。

        落雪 CDN 上 ``plate/1`` 是一张纯白空板（有色像素为 0），
        若拿它当兜底，所有未公开名牌的玩家都会渲染成一片白，
        看起来就像「收藏品没显示」。这里用彩色像素数量做过滤。
        """
        try:
            im = Image.open(path_str).convert("RGB")
            arr = np.asarray(im, dtype=np.int16)
            colored = (arr.std(axis=2) > 20).sum()
            return bool(colored > 500)
        except Exception:
            return False

    @classmethod
    def _default_plate(cls) -> Optional[Image.Image]:
        """挑一张有内容的底板作为兜底（跳过 CDN 上的空白板）。

        优先用 ``CHU_DEFAULT_NAME_PLATE`` 配置，其次按 ID 升序挑第一张
        真正有画面的底板，避免落到 plate/1 这类纯白空板。
        """
        preferred = getattr(config, "chu_default_name_plate", None)
        if preferred:
            p = PLATE_DIR / f"{preferred}.png"
            if p.exists() and cls._plate_has_content(str(p)):
                return Image.open(p).convert("RGBA")

        numeric = sorted(
            (p for p in PLATE_DIR.glob("*.png") if p.stem.isdigit()),
            key=lambda p: int(p.stem),
        )
        for p in numeric:
            if cls._plate_has_content(str(p)):
                return Image.open(p).convert("RGBA")
        return None

    def _pick_name_plate(self, player: PlayerChuInfo) -> Optional[Image.Image]:
        """挑选名牌底板（收藏品）。

        优先级：按 QQ 号配置 > 全局配置 > API 返回的 name_plate_id > 自动兜底。
        落雪开发者接口**只在玩家公开名牌时**才返回 ``name_plate``，
        不公开的玩家走自动兜底，会挑一张真实有画面的底板，而不是 CDN 上
        那张纯白的 ``plate/1``。返回 None 表示使用纯色底板。
        """
        by_user = config.chu_name_plate_by_user or {}
        uid = str(player.user_id or "")
        if uid and uid in by_user:
            plate_id = by_user[uid]
        elif config.chu_name_plate_id is not None:
            plate_id = config.chu_name_plate_id
        elif player.name_plate_id:
            plate_id = player.name_plate_id
        else:
            plate_id = None

        if plate_id == 0:
            return None

        if plate_id is not None:
            p = PLATE_DIR / f"{plate_id}.png"
            if p.exists() and self._plate_has_content(str(p)):
                return Image.open(p).convert("RGBA")
            logger.debug(
                f"[名片] 底板 {plate_id} 不存在或无内容，改用默认底板"
            )

        return self._default_plate()


    @staticmethod
    def _star_points(cx: float, cy: float, r_out: float, r_in: float, n: int = 5) -> list[tuple[float, float]]:
        """生成五角星顶点，尖角朝上。"""
        import math

        pts = []
        for i in range(n * 2):
            r = r_out if i % 2 == 0 else r_in
            a = -math.pi / 2 + i * math.pi / n
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        return pts

    def _draw_reborn_star(self, draw: ImageDraw.ImageDraw, x: int, y: int, size: int, count: int, hidden: bool) -> None:
        """画 Lv 左上角的转生星星，星内数字 = reborn_count（1 = 100 级）。

        玩家选择不公开等级时，星内显示 ** 而不是数字。
        """
        cx, cy = x + size / 2, y + size / 2
        pts = self._star_points(cx, cy, size / 2, size / 2 * 0.46)
        draw.polygon(pts, fill=(255, 196, 60, 255), outline=(150, 100, 20, 255))
        # 内圈描边让星星更立体
        inner = self._star_points(cx, cy, size / 2 * 0.72, size / 2 * 0.34)
        draw.line(inner + [inner[0]], fill=(255, 235, 170, 180), width=1)
        txt = "**" if hidden else str(count)
        font = self._f(FONT_NUM, max(8, int(size * 0.46)))
        tw = draw.textlength(txt, font=font)
        draw.text((cx - tw / 2, cy - size * 0.30), txt, fill=(110, 60, 10, 255), font=font)

    def _draw_class_badge(
        self,
        canvas: Image.Image,
        x: int,
        y: int,
        base: int,
        medal: int,
        ribbon_h: int = 15,
    ) -> None:
        """在 RATING 右侧画 CLASS 段位徽章：缎带打底 + 勋章居中叠加。

        落雪 ``class_emblem`` 给的是两个档位号：``base`` 选缎带底色（1~6：
        蓝/绿/黄/红/紫/彩虹），``medal`` 选勋章（1~6 对应罗马数字 I~VI）。
        素材取自 ``static/chu/class/{base,medal}-N.webp``，缎带 278x80、
        勋章 184x140，勋章按比例缩到缎带高度的 1.9 倍并垂直居中压在缎带上，
        和游戏内「缎带 + 勋章」的观感一致。素材缺失时静默跳过，不影响出图。
        """
        b_idx = min(max(base, 1), 6)
        m_idx = min(max(medal, 1), 6)

        ribbon_path = CLASS_DIR / f"base-{b_idx}.webp"
        medal_path = CLASS_DIR / f"medal-{m_idx}.webp"

        ribbon = None
        if ribbon_path.exists():
            ribbon = Image.open(ribbon_path).convert("RGBA")
            rw = ribbon_h * ribbon.width / ribbon.height
            ribbon = ribbon.resize((int(rw), ribbon_h), Resampling.LANCZOS)

        medal_img = None
        if medal_path.exists():
            medal_img = Image.open(medal_path).convert("RGBA")
            mh = int(ribbon_h * 1.9)
            mw = int(mh * medal_img.width / medal_img.height)
            medal_img = medal_img.resize((mw, mh), Resampling.LANCZOS)

        if ribbon is None and medal_img is None:
            return

        # 整体高度由较高的勋章决定，缎带在垂直方向居中对齐
        badge_h = medal_img.height if medal_img is not None else ribbon.height
        if ribbon is not None:
            ry = y + (badge_h - ribbon.height) // 2
            canvas.alpha_composite(ribbon, (x, ry))

        # 勋章压在缎带左端折角附近，与游戏内位置一致
        if medal_img is not None:
            mx = x + (int(ribbon.width * 0.16) if ribbon is not None else 0)
            my = y + (badge_h - medal_img.height) // 2
            canvas.alpha_composite(medal_img, (mx, my))

    def _draw_header(self, player: PlayerChuInfo) -> None:
        # Player name card — 用玩家的名牌底板做卡片画布（收藏品个性化）
        SCALING_FACTOR = 0.75  # 姓名框缩放比例
        plate_raw = self._pick_name_plate(player)
        if plate_raw is None:
            plate_raw = Image.new("RGBA", (576, 228), (238, 236, 246, 235))
        plate_size = [int(x * SCALING_FACTOR) for x in plate_raw.size]
        card = plate_raw.resize(plate_size, Resampling.LANCZOS)  # 75%

        # 彩色底板上叠加一层半透明白，保证 Lv / 名字 / RATING 可读
        wash = Image.new("RGBA", card.size, (255, 255, 255, 96))
        card = Image.alpha_composite(card, wash)

        # ── Character icon: bottom-right, 40% of card height ──
        icon_size = int(card.height * 0.40)
        icon_path = ICON_DIR / f"{player.character_id or 1}.png"
        if not icon_path.exists():
            icon_path = ICON_DIR / "CHU_UI_Character_0000_00_02.png"
        if icon_path.exists():
            icon_raw = Image.open(icon_path).convert("RGBA")
            icon_disp = icon_raw.resize((icon_size, icon_size), Resampling.LANCZOS)
            icon_x, icon_y = 351, 64
            card.alpha_composite(icon_disp, (icon_x, icon_y))

        # ── Player info panel: draw text on top_panel, then paste onto card ──
        top_panel_path = PIC_DIR / "top_panel_background_restored.png"
        top_panel_raw = Image.open(top_panel_path).convert("RGBA")
        top_panel_size = [int(x * SCALING_FACTOR) for x in top_panel_raw.size]
        top_panel = top_panel_raw.resize(top_panel_size, Resampling.LANCZOS)
        PANEL_W, PANEL_H = top_panel.size

        # Line 1: Lv.XX  Name  (top 60% of panel)
        line1_h = int(PANEL_H * 0.6)
        line1_y = 13
        lv_font = self._f(FONT_MAIN, 13)
        lvn_font = self._f(FONT_MAIN, 20)
        nm_font = self._f(FONT_MAIN, 23)

        pd = ImageDraw.Draw(top_panel)
        lv_text = "Lv." if not player.level_hidden else "Lv. "
        lvn_text = "**" if player.level_hidden else str(player.level)
        display_name = player.name
        lv_w = int(pd.textlength(lv_text, font=lv_font))
        lvn_w = int(pd.textlength(lvn_text, font=lvn_font))

        # 转生星星：位于 Lv 数字左上角，星内数字 = reborn_count（1 代表 100 级）。
        # 星心放在「Lv.」标签右侧、数字左上角之外，避免压住等级数字。
        star_size = 18
        star_x = 12 + lv_w - 8
        star_y = line1_y - 8
        show_star = player.reborn_count > 0 or player.level_hidden
        if show_star:
            self._draw_reborn_star(
                pd, star_x, star_y, star_size, player.reborn_count, player.level_hidden
            )
        name_x = 12 + lv_w + lvn_w + 8
        name_text = self._trunc(display_name, PANEL_W - name_x - 10, nm_font, pd)

        pd.text((12, line1_y + 10), lv_text, fill="black", font=lv_font)
        pd.text((12 + lv_w, line1_y + 3), lvn_text, fill="black", font=lvn_font)
        pd.text((name_x, line1_y), name_text, fill="black", font=nm_font)

        # Line 2: RATING label image + digit images (bottom 40% of panel)
        line2_y = line1_h + 5
        rating_color = get_rating_color_name(player.rating)

        # Rating label image
        rating_label_path = PIC_DIR / f"rating_{rating_color}.png"
        if rating_label_path.exists():
            rating_label = Image.open(rating_label_path).convert("RGBA")
            rating_label = rating_label.resize((56, 13), Resampling.LANCZOS)
            top_panel.paste(rating_label, (8, line2_y + 3), rating_label)

        # Rating digit images (18×24 each)
        rating_str = f"{player.rating:.2f}"
        digit_x = 80
        digit_y = line2_y  # vertically center with label
        for ch in rating_str:
            fname = f"point_{rating_color}.png" if ch == "." else f"{ch}_{rating_color}.png"
            digit_path = PIC_DIR / fname
            if digit_path.exists():
                digit_img = Image.open(digit_path).convert("RGBA")
                digit_img = digit_img.resize((12, 16), Resampling.LANCZOS)
                top_panel.paste(digit_img, (digit_x, digit_y), digit_img)
            digit_x += 12

        # CLASS 段位徽章：缎带（base）打底 + 勋章（medal）居中叠在上方。
        # 勋章比缎带高，所以整组往上提，让「勋章中心」与 RATING 数字行对齐，
        # 否则勋章底边会顶到面板下沿。
        if player.class_emblem_base or player.class_emblem_medal:
            badge_h = int(18 * 1.9)  # 缎带 18px、勋章 1.9 倍高，见 _draw_class_badge
            self._draw_class_badge(
                top_panel,
                digit_x + 10,
                line2_y + 8 - badge_h // 2,
                player.class_emblem_base,
                player.class_emblem_medal,
                ribbon_h=18,
            )

        place_panel_x = plate_size[0] - icon_size - top_panel_size[0] - int(plate_size[0] * 0.05)
        place_panel_y = icon_y + 3
        card.alpha_composite(top_panel, (place_panel_x, place_panel_y))

        # ── Trophy title bar (top of card, full width) ──
        cd = ImageDraw.Draw(card)
        trophy_x, trophy_y = 384, 36  # 50%
        if player.trophy:
            color_idx = TROPHY_COLOR_INDEX.get(player.trophy.color, 0)
            trophy_bg_path = PIC_DIR / f"CHU_UI_Trophy_{color_idx}.png"
            if not trophy_bg_path.exists():
                raise FileNotFoundError(f"称号背景文件 {trophy_bg_path} 不存在, 请检查资源文件路径")
            timg = Image.open(trophy_bg_path).convert("RGBA")
            timg = timg.resize((trophy_x, trophy_y), Resampling.LANCZOS)
            place_trophy_x = place_panel_x - 32
            place_trophy_y = place_panel_y - trophy_y + 3
            card.alpha_composite(timg, (place_trophy_x, place_trophy_y))

            trophy_font = self._f(FONT_MAIN, 12)
            name_w = cd.textlength(player.trophy.name, font=trophy_font)
            name_x = place_trophy_x + (trophy_x - int(name_w)) // 2
            name_y = place_trophy_y + (trophy_y - 16) // 2
            cd.text((name_x, name_y), player.trophy.name, fill=(0, 0, 0), font=trophy_font)

        # ── Paste card onto header ──
        card_paste_x = 18
        card_paste_y = (self.header_h - card.height) // 2 + 30
        self._im.alpha_composite(card, (card_paste_x, card_paste_y))

        # ── 收藏品信息条（卡片右侧空白区）──
        # 开发者 API 能拿到的收藏品：角色、CLASS 勋章、转生次数、
        # 总游玩次数、OVER POWER、RATING 称号。这里做成两列胶囊，
        # 让名片不再是光秃秃的一块底板。
        self._draw_collectibles(player, card_paste_x + card.width + 18, card_paste_y, card.height)

        # ── Logo top-right ────────────────────────────────────────────
        # 2027 版 logo 是宽幅素材（约 2.8:1），按宽度等比缩放后垂直居中，
        # 不能像旧版正方形那样固定 300x300，否则会被压扁。
        logo_path = PIC_DIR / "chunithm_cn_2027.png"
        if logo_path.exists():
            logo_raw = Image.open(logo_path).convert("RGBA")
            logo_w = 300
            logo_h = round(logo_raw.height * logo_w / logo_raw.width)
            logo_img = logo_raw.resize((logo_w, logo_h), Resampling.LANCZOS)
            logo_x = self.width - logo_w - 20
            logo_y = (self.header_h - logo_h) // 2
            self._im.alpha_composite(logo_img, (logo_x, logo_y))

    def _draw_collectibles(self, player: PlayerChuInfo, x: int, y: int, h: int) -> None:
        """在名片右侧画「收藏品 / 玩家资料」信息条。

        内容全部来自落雪开发者接口确实返回的字段，不编造数据：
        角色（名称+等级）、CLASS 勋章、转生次数、当前等级、总游玩次数、
        OVER POWER（含当前段位的达成百分比）。
        """
        items: list[tuple[str, str]] = []
        if player.character_name:
            label = player.character_name
            if player.character_level:
                label = f"{label} · Lv{player.character_level}"
            items.append(("CHARACTER", label))
        if player.class_emblem_base or player.class_emblem_medal:
            items.append(("CLASS", f"base {player.class_emblem_base} · medal {player.class_emblem_medal}"))
        items.append(("REBORN", "**" if player.level_hidden else f"× {player.reborn_count}"))
        items.append(("LEVEL", "**" if player.level_hidden else str(player.level)))
        items.append(("PLAY COUNT", f"{player.total_play_count:,}"))
        if player.over_power:
            # OVER POWER 后面跟当前段位的达成百分比，形如 "50,491 (43%)"
            if player.over_power_progress:
                items.append((
                    "OVER POWER",
                    f"{player.over_power:,.2f} ({player.over_power_progress:.2f}%)",
                ))
            else:
                items.append(("OVER POWER", f"{player.over_power:,.2f}"))
        if not items:
            return

        label_font = self._f(FONT_MAIN, 9)
        value_font = self._f(FONT_NUM, 12)
        rows = len(items)
        box_w = 360
        panel_h = h
        pad = 8
        row_h = (panel_h - pad * 2) // rows

        # 实体白底面板，避免长文本直接落在花哨背景上看不清
        panel = Image.new("RGBA", (box_w, panel_h), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel)
        pd.rounded_rectangle(
            (0, 0, box_w - 1, panel_h - 1), radius=12, fill=(255, 255, 255, 222)
        )
        self._im.alpha_composite(panel, (x, y))
        draw = ImageDraw.Draw(self._im)

        for i, (label, value) in enumerate(items):
            ry = y + pad + i * row_h
            lw = draw.textlength(label, font=label_font)
            avail = box_w - 24 - lw - 8
            value = self._trunc(value, int(max(20, avail)), value_font, draw)
            draw.text((x + 12, ry + 4), label, fill=(150, 142, 172, 255), font=label_font)
            vw = draw.textlength(value, font=value_font)
            draw.text((x + box_w - vw - 12, ry + 2), value, fill=(46, 42, 64, 255), font=value_font)
            if i < rows - 1:
                draw.line(
                    (x + 12, ry + row_h - 2, x + box_w - 12, ry + row_h - 2),
                    fill=(220, 216, 232, 160),
                    width=1,
                )

    def _draw_footer(self) -> None:
        fy = self.height - self.footer_h + 4
        # self._draw.line((14, fy, self.width - 14, fy), fill=(255, 255, 255, 25), width=1)
        font = self._f(FONT_NUM, 12)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._draw.text((18, fy + 12), ts, fill=(120, 112, 145, 255), font=font)
        rt = "Powered By maimai.lxns.net / Generated By Rikka"
        rw = self._draw.textlength(rt, font=font)
        self._draw.text((self.width - rw - 18, fy + 12), rt, fill=(120, 112, 145, 255), font=font)

    CARD_RADIUS = 12

    # 文字配色
    TEXT_DARK = (46, 42, 64, 255)
    TEXT_MUTED = (138, 130, 158, 255)

    # 曲绘贴满卡片左侧，四周露出的难度色边框宽度；圆角跟随 CARD_RADIUS
    COVER_BORDER_WIDTH = 4

    # ── iOS 液态玻璃材质参数 ──────────────────────────────────
    # 基底：背景模糊 → 去色 → 混中性冷白 → 高不透明度，保证文字可读
    GLASS_BLUR = 16
    GLASS_NEUTRAL = (232, 236, 244)
    GLASS_DESATURATE = 0.86
    GLASS_WHITE = (250, 252, 255)
    GLASS_WHITE_MIX = 0.52
    GLASS_ALPHA = 0.94
    GLASS_SHADOW = (70, 60, 100, 45)
    # 边缘彩色折射：四层错位描边 (颜色, 偏移)
    GLASS_PRISM_LAYERS = (
        ((120, 175, 255, 150), (-2, -2)),   # 左上 青蓝
        ((190, 140, 255, 140), (3, -1)),    # 右上 紫
        ((255, 145, 205, 135), (2, 3)),     # 右下 粉
        ((135, 225, 235, 130), (-2, 2)),    # 左下 青
    )
    GLASS_PRISM_BLUR = 4
    # 上沿弯月形镜面高光
    GLASS_SHEEN_ALPHA = 120
    GLASS_SHEEN_BLUR = 7
    GLASS_SHEEN_FALLOFF = 2.6

    # 评级文字配色：SSS 系金色、SS 紫色、S 系蓝色、其余灰色
    RANK_COLORS = {
        "SSS+": (245, 180, 45),
        "SSS": (245, 180, 45),
        "SS+": (245, 180, 45),
        "SS": (176, 124, 232),
        "S+": (108, 156, 240),
        "S": (108, 156, 240),
        "AAA": (120, 190, 130),
        "AA": (120, 190, 130),
    }

    @staticmethod
    def _diff_color(score: PlayerChuScore) -> tuple:
        """取谱面难度对应的主色：BASIC 绿 / ADVANCED 黄 / EXPERT 红 / MASTER 紫 / ULTIMA 黑。"""
        idx = int(getattr(score.song_difficulty, "value", 0) or 0)
        if not 0 <= idx < len(DIFF_COLORS):
            idx = 0
        return DIFF_COLORS[idx]

    @staticmethod
    def _tint(color: tuple, factor: float = 0.16) -> tuple:
        """把主色和白色按比例混合，得到同色系的淡色（用作底框填充）。"""
        return tuple(int(c * factor + 255 * (1 - factor)) for c in color[:3])

    def _draw_score_card(self, score: PlayerChuScore, rank_num: int, x: int, y: int) -> None:
        """绘制单条成绩卡：iOS 液态玻璃圆角卡 + 左侧难度色边框曲绘 + 右侧文字。

        玻璃材质由「中性乳白基底 + 上沿弯月高光 + 边缘彩色折射 + 内侧亮边 +
        柔阴影」组成，不直接透出花哨背景，保证文字可读。难度主色
        (BASIC 绿 / ADVANCED 黄 / EXPERT 红 / MASTER 紫 / ULTIMA 灰) 只描在
        曲绘外框上；定数用同色文字 + 同色淡底胶囊。
        """
        cw, ch = self._card_width, self._card_height
        card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)
        diff_color = self._diff_color(score)
        R = self.CARD_RADIUS

        # ── 0) 外侧柔阴影 ──
        cd.rounded_rectangle((2, 5, cw, ch + 2), radius=R + 2, fill=self.GLASS_SHADOW)

        # ── 1) 玻璃基底：模糊背景 → 大幅去色 → 混中性乳白 → 高不透明度 ──
        # 直接透背景会被满屏高饱和圆点染色、文字发花，所以这里去色 86%，
        # 只留极淡的环境色，得到接近 iOS 的干净冷白玻璃。
        pad = 4
        bg_box = (max(0, x - pad), max(0, y - pad), x + cw + pad, y + ch + pad)
        bg = self._im.crop(bg_box).convert("RGBA")
        bw_px, bh_px = bg.size
        blurred = bg.filter(ImageFilter.GaussianBlur(self.GLASS_BLUR))
        neutral = Image.new("RGB", (bw_px, bh_px), self.GLASS_NEUTRAL)
        desat = Image.blend(blurred.convert("RGB"), neutral, self.GLASS_DESATURATE).convert("RGBA")
        glass = Image.blend(
            desat,
            Image.new("RGBA", (bw_px, bh_px), (*self.GLASS_WHITE, 255)),
            self.GLASS_WHITE_MIX,
        )

        gm = Image.new("L", (bw_px, bh_px), 0)
        ImageDraw.Draw(gm).rounded_rectangle(
            (pad, pad, pad + cw - 2, pad + ch - 3), radius=R, fill=255
        )
        a = glass.getchannel("A").point(lambda v: int(v * self.GLASS_ALPHA))
        glass.putalpha(ImageChops.multiply(a, gm))
        card.alpha_composite(glass, (-pad, -pad))

        # ── 2) 边缘彩色折射色散（棱镜）──
        # 四层不同颜色、略微错位的圆角描边，模糊后只保留边缘，
        # 模拟玻璃边缘把环境光拆成蓝/紫/粉/青的效果。
        prism = Image.new("RGBA", (cw + 12, ch + 12), (0, 0, 0, 0))
        pd = ImageDraw.Draw(prism)
        for color, (dx, dy) in self.GLASS_PRISM_LAYERS:
            pd.rounded_rectangle(
                (6 + dx, 6 + dy, 6 + cw - 2 + dx, 6 + ch - 3 + dy),
                radius=R + 1, outline=color, width=3,
            )
        prism = prism.filter(ImageFilter.GaussianBlur(self.GLASS_PRISM_BLUR))
        outer = Image.new("L", (cw + 12, ch + 12), 0)
        ImageDraw.Draw(outer).rounded_rectangle(
            (6, 6, 6 + cw - 2, 6 + ch - 3), radius=R + 1, fill=255
        )
        inner = Image.new("L", (cw + 12, ch + 12), 0)
        ImageDraw.Draw(inner).rounded_rectangle(
            (10, 10, 2 + cw - 2, 2 + ch - 3), radius=max(2, R - 4), fill=255
        )
        edge = ImageChops.subtract(outer, inner)
        prism.putalpha(ImageChops.multiply(prism.getchannel("A"), edge))
        card.alpha_composite(prism, (-6, -6))

        # ── 3) 上沿弯月形镜面高光 ──
        # 一个比卡片更宽的扁椭圆，只露出上半部分 → 弧形高光带；
        # 再用纵向渐变让它向下快速衰减，避免整片发白。
        sheen = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        sd = ImageDraw.Draw(sheen)
        sd.ellipse(
            (-cw * 0.15, -ch * 0.92, cw * 1.15, ch * 0.62),
            fill=(255, 255, 255, self.GLASS_SHEEN_ALPHA),
        )
        sheen = sheen.filter(ImageFilter.GaussianBlur(self.GLASS_SHEEN_BLUR))
        grad = Image.new("L", (1, ch), 0)
        for yy in range(ch):
            t = yy / max(1, ch - 1)
            grad.putpixel((0, yy), int(255 * max(0.0, 1.0 - t * self.GLASS_SHEEN_FALLOFF)))
        grad = grad.resize((cw, ch))
        sm = Image.new("L", (cw, ch), 0)
        ImageDraw.Draw(sm).rounded_rectangle((0, 0, cw - 2, ch - 3), radius=R, fill=255)
        sheen.putalpha(
            ImageChops.multiply(
                ImageChops.multiply(sheen.getchannel("A"), grad), sm
            )
        )
        card.alpha_composite(sheen)

        # ── 4) 内侧亮白细描边 + 底部内阴影（玻璃厚度感）──
        cd.rounded_rectangle(
            (1, 1, cw - 3, ch - 4), radius=R,
            outline=(255, 255, 255, 215), width=2,
        )
        cd.rounded_rectangle(
            (2, ch - 12, cw - 3, ch - 4), radius=8,
            outline=(150, 145, 175, 45), width=2,
        )

        # ── 曲绘：贴满卡片左侧，四角圆角，外侧一圈难度色边框 ──
        # 曲绘顶到卡片边缘（无留白），外框圆角与卡片圆角一致；
        # 先画实心难度色圆角底，再把略小的曲绘盖上去，露出的边缘即边框。
        bw = self.COVER_BORDER_WIDTH
        outer_size = ch - 3
        inner_size = outer_size - 2 * bw
        outer_x, outer_y = 0, 0
        inner_x, inner_y = bw, bw

        cp = COVER_DIR / f"{score.song_id}.png"
        if cp.exists():
            cover = Image.open(cp).convert("RGBA").resize((inner_size, inner_size), Resampling.LANCZOS)
        else:
            cover = Image.new("RGBA", (inner_size, inner_size), (224, 218, 240, 255))

        # 难度色底框：与卡片同圆角，贴满左侧
        cd.rounded_rectangle(
            (outer_x, outer_y, outer_x + outer_size - 1, outer_y + outer_size - 1),
            radius=self.CARD_RADIUS,
            corners=(True, True, True, True),
            fill=(*diff_color[:3], 255),
        )
        # 曲绘本体：四圆角 mask，比底框小一圈
        mask = Image.new("L", (inner_size, inner_size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, inner_size - 1, inner_size - 1),
            radius=max(2, self.CARD_RADIUS - bw),
            corners=(True, True, True, True),
            fill=255,
        )
        card.paste(cover, (inner_x, inner_y), mask)

        # ── 右侧文字区 ──
        tx = outer_x + outer_size + 10
        max_w = cw - tx - 8

        # 序号 #N（右上角，浅灰小字）；先算宽度，曲名要给序号让位
        idx_font = self._f(FONT_NUM, 9)
        idx_txt = f"#{rank_num}"
        idx_w = int(cd.textlength(idx_txt, idx_font))
        cd.text((cw - idx_w - 8, 7), idx_txt, fill=self.TEXT_MUTED, font=idx_font)

        # 曲名（单行，放不下就截断加省略号）
        name_font = self._f(FONT_MAIN, 12)
        name_avail = max_w - idx_w - 8
        name_txt = self._trunc(score.song_name, name_avail, name_font, cd)
        cd.text((tx, 6), name_txt, fill=self.TEXT_DARK, font=name_font)

        # 分数（大号）+ 评级文字
        # /scores 端点只有评级、没有分数，这类成绩显示 "—" 而不是假的 0。
        has_score = score.score > 0
        rank_str = score_to_rank(score.score) if has_score else ""
        rank_color = self.RANK_COLORS.get(rank_str, self.TEXT_MUTED)
        score_font = self._f(FONT_NUM, 20)
        score_txt = self._score_str(score.score) if has_score else "—"
        score_y = 25
        cd.text((tx, score_y), score_txt, fill=self.TEXT_DARK, font=score_font)
        sw = cd.textlength(score_txt, font=score_font)
        if rank_str:
            rank_font = self._f(FONT_NUM, 11)
            cd.text((tx + sw + 4, score_y + 9), rank_str, fill=rank_color, font=rank_font)

        # ── 定数徽章：难度主色文字 + 同色淡底圆角胶囊 ──
        song_diff = self._get_song_level_value(score)
        pill_font = self._f(FONT_NUM, 11)
        pill_txt = song_diff
        pill_tw = cd.textlength(pill_txt, font=pill_font)
        pill_h = 17
        pill_w = int(pill_tw) + 13
        pill_y = ch - pill_h - 9
        cd.rounded_rectangle(
            (tx, pill_y, tx + pill_w, pill_y + pill_h),
            radius=pill_h // 2,
            fill=self._tint(diff_color),
        )
        # 文字垂直居中：11px 字体可视高度约 11，居中留 3px
        cd.text((tx + 6.5, pill_y + 2), pill_txt, fill=(*diff_color[:3], 255), font=pill_font)

        # 定数后面的单曲 Rating（无分数时不显示假的 0.00）
        rating_font = self._f(FONT_MAIN, 11)
        rating_txt = f"→ {score.rating:.2f}" if score.rating > 0 else ""
        cd.text(
            (tx + pill_w + 5, pill_y + 3),
            rating_txt,
            fill=self.TEXT_MUTED,
            font=rating_font,
        )

        # 达成标记（右下角）：按 full_combo 的真实类型显示 AJC / AJ / FC，
        # 不能只判断「full_combo 非空」——那样 ALL JUSTICE 会被误显示成 FC。
        mark = None
        fc = score.full_combo.value if score.full_combo else None
        if fc == "alljusticecritical":
            mark = "AJC"
        elif fc == "alljustice":
            mark = "AJ"
        elif fc == "fullcombo":
            mark = "FC"
        elif score.full_chain:
            mark = "AJ"  # FULL CHAIN 属于 ALL JUSTICE 系
        if mark:
            mfont = self._f(FONT_NUM, 10)
            mw = cd.textlength(mark, font=mfont)
            cd.text((cw - mw - 8, pill_y + 4), mark, fill=self.TEXT_MUTED, font=mfont)


        self._im.alpha_composite(card, (x, y))

    def _draw_section_title(self, y: int, title: str, avg: float, accent: tuple) -> None:
        """分区标题条：半透明白色圆角条 + 左侧彩色圆点 + 深色文字。"""
        draw = ImageDraw.Draw(self._im)
        x0, x1 = self.section_pad_x, self.width - self.section_pad_x

        bar = Image.new("RGBA", (x1 - x0 + 1, self.section_height), (0, 0, 0, 0))
        bd = ImageDraw.Draw(bar)
        bd.rounded_rectangle(
            (0, 0, x1 - x0, self.section_height - 1),
            radius=self.section_height // 2,
            fill=(255, 255, 255, 224),
            outline=(*accent, 120),
            width=2,
        )
        self._im.alpha_composite(bar, (x0, y))

        # 左侧彩色圆点
        cy = y + self.section_height // 2
        draw.ellipse((x0 + 16, cy - 7, x0 + 30, cy + 7), fill=accent)

        # 标题（左对齐，深色）
        draw.text((x0 + 40, y + 9), title, fill=(48, 42, 68, 255), font=self._f(FONT_TITLE, 22))

        # AVG（右侧，彩色）
        avg_font = self._f(FONT_NUM, 19)
        avg_txt = f"AVG {avg:.2f}"
        aw = draw.textlength(avg_txt, font=avg_font)
        draw.text((x1 - aw - 22, y + 11), avg_txt, fill=(*accent, 255), font=avg_font)

    def _draw_section(self, y0: int, title: str, scores: list[PlayerChuScore], limit: int, accent: tuple) -> int:
        """Render one section. Returns y position immediately after."""
        avg = sum(s.rating for s in scores) / len(scores) if scores else 0.0
        self._draw_section_title(y0, title, avg, accent)

        grid_y = y0 + self.section_height + 4
        for idx, sc in enumerate(scores[:limit]):
            col = idx % self._col
            row = idx // self._col
            cx = self.section_pad_x + col * (self._card_width + GAP_X)
            cy = grid_y + row * (self._card_height + GAP_Y)
            self._draw_score_card(sc, idx + 1, cx, cy)

        rows = math.ceil(min(len(scores), limit) / self._col)
        return grid_y + rows * (self._card_height + GAP_Y)
