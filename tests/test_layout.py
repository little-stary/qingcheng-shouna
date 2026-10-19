# -*- coding: utf-8 -*-
"""窗口尺寸计算的测试：覆盖 100% / 125% / 150% 缩放。

思路：Windows 的缩放比例，对 Tk 来说等于"屏幕的逻辑像素变小"。
    1920×1080 在 150% 下，Tk 看到的是 1280×720。
所以只要验证「给不同的屏幕尺寸，窗口都不会超出、也不会小到挤坏布局」就够了。

说明：本机没有真的去改系统缩放设置（那属于修改系统设置），
这里用等价的逻辑屏幕尺寸来验证。
"""

import unittest

from app.config import MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH, WINDOW_HEIGHT, WINDOW_WIDTH
from app.ui.layout import clamp_minsize, compute_geometry, compute_window_position, compute_window_size


class TestWindowSize(unittest.TestCase):
    def test_100_percent_1080p(self):
        # 1920x1080 / 100% -> 逻辑 1920x1080
        self.assertEqual(compute_window_size(1920, 1080), (WINDOW_WIDTH, WINDOW_HEIGHT))

    def test_125_percent_1080p(self):
        # 1920x1080 / 125% -> 逻辑 1536x864
        w, h = compute_window_size(1536, 864)
        self.assertEqual(w, WINDOW_WIDTH)
        self.assertLessEqual(h, 864 - 40)

    def test_150_percent_1080p(self):
        # 1920x1080 / 150% -> 逻辑 1280x720，这是最容易出问题的一档
        w, h = compute_window_size(1280, 720)
        self.assertEqual(w, WINDOW_WIDTH)
        self.assertLessEqual(h, 720 - 80, "窗口不能高到超出屏幕")
        self.assertGreaterEqual(h, MIN_WINDOW_HEIGHT, "也不能小到把布局挤坏")

    def test_100_percent_768p_laptop(self):
        # 1366x768 / 100% -> 逻辑 1366x768，常见的笔记本
        w, h = compute_window_size(1366, 768)
        self.assertEqual(w, WINDOW_WIDTH)
        self.assertLessEqual(h, 768)

    def test_small_screen_never_clipped(self):
        # 屏幕再小，窗口也不能比屏幕还大
        for sw, sh in [(1280, 720), (1024, 600), (800, 600)]:
            w, h = compute_window_size(sw, sh)
            self.assertLessEqual(w, sw, f"{sw}x{sh} 宽度超出")
            self.assertLessEqual(h, sh, f"{sw}x{sh} 高度超出")

    def test_never_smaller_than_minimum_on_normal_screen(self):
        w, h = compute_window_size(1920, 1080)
        self.assertGreaterEqual(w, MIN_WINDOW_WIDTH)
        self.assertGreaterEqual(h, MIN_WINDOW_HEIGHT)


class TestPositionAndGeometry(unittest.TestCase):
    def test_position_inside_screen(self):
        x, y = compute_window_position(1920, 1080, 940, 720)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)
        self.assertLessEqual(x + 940, 1920)
        self.assertLessEqual(y + 720, 1080)

    def test_position_never_negative(self):
        x, y = compute_window_position(800, 600, 880, 680)
        self.assertEqual((x, y), (0, 0))

    def test_geometry_string_format(self):
        text = compute_geometry(1920, 1080)
        self.assertRegex(text, r"^\d+x\d+\+\d+\+\d+$")

    def test_minsize_not_bigger_than_screen(self):
        w, h = clamp_minsize(800, 600)
        self.assertLessEqual(w, 800)
        self.assertLessEqual(h, 600)


if __name__ == "__main__":
    unittest.main()
