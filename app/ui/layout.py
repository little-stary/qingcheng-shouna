# -*- coding: utf-8 -*-
"""窗口尺寸计算：让界面在任何缩放比例下都不超出屏幕。

为什么要单独抽一个文件？
    因为"窗口该多大"是纯计算，跟界面无关。
    抽出来以后可以直接写单元测试，覆盖 100% / 125% / 150% 各种屏幕，
    不用真的去改系统缩放（那属于修改系统设置，不能随便动）。

关键概念：
    Tk 报告的屏幕尺寸是"逻辑像素"（已按缩放折算）。
    例如 1920×1080 的屏幕在 150% 缩放下，Tk 报告的是 1280×720。
    所以窗口尺寸也必须用逻辑像素，才能和屏幕对得上。
"""

from app.config import (
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)

# 留白：窗口四周不要紧贴屏幕边缘，否则看起来像卡住了
MARGIN_W = 40
MARGIN_H = 80          # 高度多留一些给标题栏和任务栏


def compute_window_size(screen_w: int, screen_h: int) -> tuple[int, int]:
    """根据可用屏幕算出窗口宽高，保证既不超出屏幕，也不小于最小可用尺寸。"""

    width = min(WINDOW_WIDTH, screen_w - MARGIN_W)
    height = min(WINDOW_HEIGHT, screen_h - MARGIN_H)

    # 屏幕特别小时，至少保住最小尺寸（再小内容就会挤在一起）
    width = max(width, min(MIN_WINDOW_WIDTH, screen_w))
    height = max(height, min(MIN_WINDOW_HEIGHT, screen_h))

    return int(width), int(height)


def compute_window_position(screen_w: int, screen_h: int, width: int, height: int) -> tuple[int, int]:
    """窗口居中偏上放置：比正中更符合视觉习惯，也不会压住任务栏。"""

    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    return int(x), int(y)


def compute_geometry(screen_w: int, screen_h: int) -> str:
    """拼成 Tk 认识的 "宽x高+x+y" 字符串。"""

    width, height = compute_window_size(screen_w, screen_h)
    x, y = compute_window_position(screen_w, screen_h, width, height)
    return f"{width}x{height}+{x}+{y}"


def clamp_minsize(screen_w: int, screen_h: int) -> tuple[int, int]:
    """最小尺寸不能超过屏幕，否则用户拖也拖不小。"""

    return (
        min(MIN_WINDOW_WIDTH, screen_w),
        min(MIN_WINDOW_HEIGHT, screen_h),
    )
