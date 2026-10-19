# -*- coding: utf-8 -*-
"""路径工具的测试：短路径显示、目录内判断。"""

import unittest

from app.utils.paths import is_inside, shorten_path


class TestShortenPath(unittest.TestCase):
    def test_short_path_untouched(self):
        path = r"D:\下载\a.pdf"
        self.assertEqual(shorten_path(path), path)

    def test_long_path_shortened(self):
        path = "D:\\" + "\\".join(["很长的文件夹名"] * 8) + "\\文件.pdf"
        result = shorten_path(path, 40)
        self.assertLessEqual(len(result), 41)
        self.assertIn("…", result)

    def test_keeps_drive(self):
        path = "C:\\" + "a" * 80 + "\\x.txt"
        self.assertTrue(shorten_path(path, 40).startswith("C:"))

    def test_keeps_file_name_tail(self):
        path = "C:\\" + "a" * 80 + "\\报告2025.pdf"
        self.assertTrue(shorten_path(path, 40).endswith("报告2025.pdf"))


class TestIsInsideSimple(unittest.TestCase):
    def test_basic(self):
        self.assertTrue(is_inside(r"C:\a", r"C:\a\b\c.txt"))
        self.assertFalse(is_inside(r"C:\a", r"C:\b\c.txt"))


if __name__ == "__main__":
    unittest.main()
