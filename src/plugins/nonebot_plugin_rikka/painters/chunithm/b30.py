"""
中二节奏 Best 30 + Selection 10 + New 20 绘图模块
"""

from PIL import Image

from ...score import PlayerChuBests, PlayerChuInfo
from ._base import GAP_Y, ScoreBaseImage

SEC_B30_COLOR = (220, 60, 50)
SEC_S10_COLOR = (220, 150, 30)
SEC_N20_COLOR = (40, 185, 110)


class DrawChuBest(ScoreBaseImage):
    def draw(self, player_info: PlayerChuInfo, bests: PlayerChuBests) -> Image.Image:
        # 画布高度按实际内容动态计算：卡片变高后写死 1800 会把 NEW 20 底部切掉。
        import math as _math

        sections = [
            (bests.bests, 30),
            (bests.selections, 10),
            (bests.new_bests, 20),
        ]
        total = self.header_h + self.section_gap + self.footer_h
        for scores, limit in sections:
            rows = _math.ceil(min(len(scores), limit) / self._col)
            total += self.section_height + 4 + rows * (self._card_height + GAP_Y) + self.section_gap
        self.height = total
        self._reset_im()

        self._draw_header(player_info)

        y = self.header_h + self.section_gap
        y = self._draw_section(y, "BEST 30", bests.bests, 30, SEC_B30_COLOR)
        y += self.section_gap
        y = self._draw_section(y, "SELECTION 10", bests.selections, 10, SEC_S10_COLOR)
        y += self.section_gap
        y = self._draw_section(y, "NEW 20", bests.new_bests, 20, SEC_N20_COLOR)

        self._draw_footer()

        return self._im
