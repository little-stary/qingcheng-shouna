# -*- coding: utf-8 -*-
"""可复用的小组件：卡片、分类统计格子。

把它们单独抽出来，主窗口的代码就只剩下"布局"和"事件处理"，读起来清楚很多。
"""

import customtkinter as ctk

from app.config import (
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_TEXT,
    COLOR_TEXT_WEAK,
)
from app.ui.theme import PAD_CARD, font


class Card(ctk.CTkFrame):
    """带标题的白色圆角卡片。

    用法：
        card = Card(parent, "第 1 步 · 选择文件夹")
        ctk.CTkLabel(card.inner, text="...").pack()
    所有内容都放进 card.inner，内边距由卡片统一控制。
    """

    def __init__(self, master, title: str | None = None, **kwargs):
        super().__init__(
            master,
            corner_radius=14,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
            **kwargs,
        )

        self.inner = ctk.CTkFrame(self, fg_color="transparent")
        self.inner.pack(fill="both", expand=True, padx=PAD_CARD, pady=PAD_CARD - 2)

        if title:
            ctk.CTkLabel(
                self.inner,
                text=title,
                font=font(13, bold=True),
                text_color=COLOR_TEXT,
                anchor="w",
            ).pack(fill="x", pady=(0, 6))


class CategoryTile(ctk.CTkFrame):
    """一个分类统计格子：上面是分类名，中间是大数字，下面是占用大小。"""

    def __init__(self, master, name: str, **kwargs):
        super().__init__(
            master,
            corner_radius=10,
            fg_color="#F8FAFC",
            border_width=1,
            border_color="#EDF1F6",
            **kwargs,
        )

        self.name_label = ctk.CTkLabel(
            self, text=name, font=font(11), text_color=COLOR_TEXT_WEAK, anchor="w"
        )
        self.name_label.pack(fill="x", padx=12, pady=(6, 0))

        self.count_label = ctk.CTkLabel(
            self, text="—", font=font(19, bold=True), text_color=COLOR_TEXT, anchor="w"
        )
        self.count_label.pack(fill="x", padx=12)

        self.size_label = ctk.CTkLabel(
            self, text=" ", font=font(10), text_color=COLOR_TEXT_WEAK, anchor="w"
        )
        self.size_label.pack(fill="x", padx=12, pady=(0, 6))

    def update_value(self, count: int, size_text: str) -> None:
        """扫描完成后更新数字。count 为 0 时用浅色显示，避免一片黑压压。"""
        self.count_label.configure(
            text=f"{count}",
            text_color=COLOR_TEXT if count else "#B9C0CC",
        )
        self.size_label.configure(text=size_text if count else " ")
