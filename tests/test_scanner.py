# -*- coding: utf-8 -*-
"""扫描器的单元测试。

重点验证三件事：
1. 分类统计对不对；
2. 子文件夹只计数、不进入；
3. 扫描前后文件"一个都没被改动"（这是第一阶段最关键的验收点）。
"""

import os
import shutil
import unittest
from pathlib import Path

from tests._cleanup import remove_tree

from app.core.scanner import format_size, scan_folder

TMP_ROOT = Path(__file__).parent / "_tmp" / "scan_case"


class TestScanFolder(unittest.TestCase):
    def setUp(self):
        # 只清理自己的子目录，不要动 tests/_tmp 下的其它东西
        # （开发时会把演示数据、临时脚本放在那里，被误删过）
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)
        TMP_ROOT.mkdir(parents=True)
        self.folder = TMP_ROOT / "样例文件夹"
        self.folder.mkdir()

        # 6 个分类各放一个文件
        (self.folder / "照片.jpg").write_bytes(b"x" * 1024)
        (self.folder / "论文.pdf").write_bytes(b"x" * 2048)
        (self.folder / "电影.mp4").write_bytes(b"x" * 4096)
        (self.folder / "压缩包.zip").write_bytes(b"x" * 512)
        (self.folder / "安装程序.exe").write_bytes(b"x" * 256)
        (self.folder / "未知文件.abc").write_bytes(b"x" * 128)

        # 一个子文件夹，里面塞一个文件：扫描时不能进去
        sub = self.folder / "子文件夹"
        sub.mkdir()
        (sub / "里面的文件.jpg").write_bytes(b"x" * 64)

    def tearDown(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)

    def test_counts(self):
        result = scan_folder(str(self.folder))
        self.assertEqual(result.total_files, 6, "只应统计第一层的 6 个文件")
        self.assertEqual(result.sub_folder_count, 1)

        counts = result.count_by_category()
        for name in ("图片", "文档", "视频", "压缩包", "安装程序", "其他"):
            self.assertEqual(counts[name], 1, name)

    def test_size(self):
        result = scan_folder(str(self.folder))
        self.assertEqual(result.total_size, 1024 + 2048 + 4096 + 512 + 256 + 128)

    def test_folder_untouched(self):
        """扫描前后：文件名、大小、修改时间完全一致，说明只读没写。"""
        before = sorted(
            (p.name, p.stat().st_size, p.stat().st_mtime)
            for p in self.folder.iterdir()
        )
        scan_folder(str(self.folder))
        after = sorted(
            (p.name, p.stat().st_size, p.stat().st_mtime)
            for p in self.folder.iterdir()
        )
        self.assertEqual(before, after)

    def test_missing_folder(self):
        with self.assertRaises(FileNotFoundError):
            scan_folder(str(TMP_ROOT / "不存在的文件夹"))

    def test_not_a_folder(self):
        file_path = TMP_ROOT / "这是一个文件.txt"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(NotADirectoryError):
            scan_folder(str(file_path))


class TestFormatSize(unittest.TestCase):
    def test_units(self):
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(999), "999 B")
        self.assertEqual(format_size(1024), "1.0 KB")
        self.assertEqual(format_size(1536), "1.5 KB")
        self.assertEqual(format_size(1024 * 1024), "1.0 MB")


MANY_ROOT = Path(__file__).parent / "_tmp" / "scan_many"


class TestScanProgressAndCancel(unittest.TestCase):
    """第三阶段新增：扫描进度回调 + 中途取消。"""

    def setUp(self):
        if MANY_ROOT.exists():
            remove_tree(MANY_ROOT)
        MANY_ROOT.mkdir(parents=True)
        for i in range(450):
            (MANY_ROOT / f"f{i}.txt").write_bytes(b"x")

    def tearDown(self):
        if MANY_ROOT.exists():
            remove_tree(MANY_ROOT)

    def test_progress_callback(self):
        seen = []
        scan_folder(str(MANY_ROOT), on_progress=seen.append)
        self.assertTrue(seen, "应当至少回调一次进度")
        self.assertGreaterEqual(max(seen), 200)

    def test_cancel_returns_partial_result(self):
        result = scan_folder(str(MANY_ROOT), should_cancel=lambda: True)
        self.assertTrue(result.cancelled, "应当标记为已取消")
        self.assertLess(result.total_files, 450, "取消后不应扫完全部文件")

    def test_no_cancel_returns_everything(self):
        result = scan_folder(str(MANY_ROOT))
        self.assertFalse(result.cancelled)
        self.assertEqual(result.total_files, 450)




if __name__ == "__main__":
    unittest.main()
