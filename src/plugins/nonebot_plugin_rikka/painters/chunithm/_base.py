import math
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from nonebot import logger
from PIL import Image, ImageDraw, ImageFont
from PIL.Image import Resampling

from ...config import config
from ...database.crud import ChuSongORM
from ...score import PlayerChuInfo, PlayerChuScore
from ._config import (
    ACH_TO_IMAGE,
    B30_BG,
    COVER_DIR,
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
        # 卡片比例对齐参考图：宽高比约 1.95:1，保证曲名与分数两行都放得下
        self._card_height = int(self._card_width / 1.95)

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
        song_id = score.song_id
        song = ChuSongORM.get_song_sync(song_id)
        if not song:
            logger.warning(f"曲目 {song_id} 不存在乐曲对象！")
            return score.song_level
        diff = score.song_difficulty.value
        level_value = float(song.difficulties.difficulties[diff].level_value)
        return str(level_value)

    def _make_bg(self) -> Image.Image:
        """生成背景。

        优先使用 static/chu/bg/b30_bg.png（浅色插画背景，参考社区成绩图风格）；
        没有该文件时回退到内置的深色点阵背景。
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

    def _draw_header(self, player: PlayerChuInfo) -> None:
        # Player name card — use plate image directly as card canvas
        SCALING_FACTOR = 0.75  # 姓名框缩放比例
        plate_path = PLATE_DIR / f"{player.name_plate_id or 1}.png"
        if not plate_path.exists():
            plate_path = PLATE_DIR / "CHU_UI_NamePlate_00000001.png"  # 576*228(实际的图像存在较大间距)
        plate_raw = Image.open(plate_path).convert("RGBA")
        plate_size = [int(x * SCALING_FACTOR) for x in plate_raw.size]
        card = plate_raw.resize(plate_size, Resampling.LANCZOS)  # 75%

        # ── Character icon: bottom-right, 40% of card height ──
        icon_size = int(card.height * 0.40)
        icon_path = ICON_DIR / f"{player.character_id or 1}.png"
        if not icon_path.exists():
            icon_path = ICON_DIR / "CHU_UI_Character_0000_00_02.png.png"
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
        lv_text = "Lv."
        lvn_text = str(player.level)
        lv_w = int(pd.textlength(lv_text, font=lv_font))
        lvn_w = int(pd.textlength(lvn_text, font=lvn_font))
        display_name = player.name
        name_text = self._trunc(display_name, PANEL_W - 12 - lv_w - lvn_w - 10, nm_font, pd)

        pd.text((12, line1_y + 10), lv_text, fill="black", font=lv_font)
        pd.text((12 + lv_w, line1_y + 3), lvn_text, fill="black", font=lvn_font)
        pd.text((12 + lv_w + lvn_w + 8, line1_y), name_text, fill="black", font=nm_font)

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
        # Place card centred vertically and 18px from the left in the header area.
        card_paste_x = 18
        card_paste_y = (self.header_h - card.height) // 2 + 30
        self._im.alpha_composite(card, (card_paste_x, card_paste_y))

        # ── Logo top-right ────────────────────────────────────────────
        logo_path = PIC_DIR / "chunithm_cn_2026.png"
        if logo_path.exists():
            logo_raw = Image.open(logo_path).convert("RGBA")
            logo_w, logo_h = 300, 300
            logo_img = logo_raw.resize((logo_w, logo_h), Resampling.LANCZOS)
            logo_x = self.width - logo_w - 20
            logo_y = (self.header_h - logo_h) // 2
            self._im.alpha_composite(logo_img, (logo_x, logo_y))

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

    # 卡片配色：白色卡面 + 深色文字，贴合参考图风格
    CARD_BG = (255, 255, 255, 244)
    CARD_SHADOW = (120, 110, 150, 30)
    TEXT_DARK = (46, 42, 64, 255)
    TEXT_MUTED = (138, 130, 158, 255)

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

    def _draw_score_card(self, score: PlayerChuScore, rank_num: int, x: int, y: int) -> None:
        """绘制单条成绩卡：白色圆角卡 + 左侧曲绘 + 右侧曲名/分数/定数。

        样式对齐社区成绩图：白卡、圆角、左侧方形曲绘，右侧从上到下是
        曲名 → 大号分数（后面跟评级）→ 定数 → 单曲 Rating。
        """
        cw, ch = self._card_width, self._card_height
        card = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        cd = ImageDraw.Draw(card)

        # ── 卡片底板（圆角 + 轻投影）──
        cd.rounded_rectangle((1, 2, cw - 1, ch - 1), radius=self.CARD_RADIUS, fill=self.CARD_SHADOW)
        cd.rounded_rectangle(
            (0, 0, cw - 2, ch - 3), radius=self.CARD_RADIUS, fill=self.CARD_BG
        )

        # ── 曲绘：左侧方形，圆角裁切 ──
        # 曲绘边长取「卡高」与「卡宽 34%」的较小值：参考图里曲绘明显小于卡高，
        # 否则文字区只剩 100 多像素，大号分数会被截断。
        pad = 6
        cover_size = min(ch - pad * 2 - 2, int(cw * 0.32))
        cp = COVER_DIR / f"{score.song_id}.png"
        if cp.exists():
            cover = Image.open(cp).convert("RGBA").resize((cover_size, cover_size), Resampling.LANCZOS)
        else:
            cover = Image.new("RGBA", (cover_size, cover_size), (224, 218, 240, 255))
        mask = Image.new("L", (cover_size, cover_size), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (0, 0, cover_size - 1, cover_size - 1), radius=self.CARD_RADIUS - 3, fill=255
        )
        card.paste(cover, (pad, (ch - cover_size) // 2), mask)

        # ── 右侧文字区 ──
        tx = pad + cover_size + 9
        max_w = cw - tx - 9

        # 序号 #N（右上角，浅灰小字）；先算宽度，曲名要给序号让位
        idx_font = self._f(FONT_NUM, 10)
        idx_txt = f"#{rank_num}"
        idx_w = int(cd.textlength(idx_txt, idx_font))
        cd.text((cw - idx_w - 9, 6), idx_txt, fill=self.TEXT_MUTED, font=idx_font)

        # 曲名：空间不够时折成两行（参考图里长曲名也是折行的），
        # 仍放不下才截断加省略号。
        name_font = self._f(FONT_MAIN, 13)
        name_avail = max_w - idx_w - 10
        name_lines = self._wrap_text(score.song_name, name_avail, name_font, cd, max_lines=2)
        for i, line in enumerate(name_lines):
            cd.text((tx, 4 + i * 16), line, fill=self.TEXT_DARK, font=name_font)

        # 分数（大号）+ 评级文字
        rank_str = score_to_rank(score.score)
        rank_color = self.RANK_COLORS.get(rank_str, self.TEXT_MUTED)
        score_font = self._f(FONT_NUM, 21)
        score_txt = self._score_str(score.score)
        score_y = 34
        cd.text((tx, score_y), score_txt, fill=self.TEXT_DARK, font=score_font)
        sw = cd.textlength(score_txt, font=score_font)
        rank_font = self._f(FONT_NUM, 12)
        # 评级与分数基线对齐
        cd.text((tx + sw + 5, score_y + 9), rank_str, fill=rank_color, font=rank_font)

        # 定数 → 单曲 Rating（贴卡片底部）
        diff_font = self._f(FONT_MAIN, 12)
        song_diff = self._get_song_level_value(score)
        cd.text((tx + 1, ch - 24), f"{song_diff} → {score.rating:.2f}", fill=self.TEXT_MUTED, font=diff_font)

        # FC / AJ 标记（右下角，浅灰）
        if score.full_combo or score.full_chain:
            mark = "FC" if score.full_combo else "AJ"
            mfont = self._f(FONT_NUM, 11)
            mw = cd.textlength(mark, font=mfont)
            cd.text((cw - mw - 9, ch - 23), mark, fill=self.TEXT_MUTED, font=mfont)

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
