# -*- coding: utf-8 -*-
"""整理方案生成器的测试。

重点验证四件事：
1. 目标路径算得对（分类文件夹 + 原文件名）；
2. 冲突能被正确标出来（同名文件 / 同名文件夹 / 目标文件夹名被文件占用）；
3. **生成方案不会创建任何文件夹**（这是本阶段的安全底线）；
4. 目标路径必须落在所选目录内部。
"""

import os
import shutil
import unittest
from pathlib import Path

from tests._cleanup import remove_tree

from app.core.classifier import classify
from app.core.models import FileItem
from app.core.planner import build_plan
from app.utils.paths import is_inside

TMP_ROOT = Path(__file__).parent / "_tmp" / "plan_case"


def make_item(path: Path) -> FileItem:
    stat = path.stat()
    return FileItem(
        path=str(path),
        name=path.name,
        ext=path.suffix.lower(),
        size=stat.st_size,
        mtime=stat.st_mtime,
        category=classify(path.name),
    )


class TestBuildPlan(unittest.TestCase):
    def setUp(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)
        TMP_ROOT.mkdir(parents=True)
        self.root = TMP_ROOT / "下载"

        self.root.mkdir()
        for name in ("a.jpg", "b.pdf", "c.mp4", "d.zip", "e.exe", "f.abc"):
            (self.root / name).write_bytes(b"x" * 10)

        # 文档 文件夹已存在，而且里面已经有一个同名 b.pdf —— 制造冲突
        (self.root / "文档").mkdir()
        (self.root / "文档" / "b.pdf").write_bytes(b"old")

        # 视频 这个名字被一个"文件"占用 —— 这一类没法建文件夹
        (self.root / "视频").write_bytes(b"i am a file")

        self.files = [make_item(p) for p in self.root.iterdir() if p.is_file()]
        self.plan = build_plan(self.files, str(self.root))

    def tearDown(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)

    def test_target_path(self):
        by_name = {i.name: i for i in self.plan.items}
        self.assertEqual(by_name["a.jpg"].dst, os.path.join(str(self.root), "图片", "a.jpg"))
        self.assertEqual(by_name["d.zip"].dst, os.path.join(str(self.root), "压缩包", "d.zip"))

    def test_conflict_same_name_file(self):
        item = {i.name: i for i in self.plan.items}["b.pdf"]
        self.assertTrue(item.conflict)
        self.assertEqual(item.conflict_kind, "同名文件")

    def test_blocked_folder(self):
        item = {i.name: i for i in self.plan.items}["c.mp4"]
        self.assertTrue(item.conflict)
        self.assertEqual(item.conflict_kind, "目标文件夹不可用")
        self.assertIn("视频", self.plan.blocked_folders)

    def test_no_conflict_for_clean_items(self):
        by_name = {i.name: i for i in self.plan.items}
        for name in ("a.jpg", "d.zip", "e.exe", "f.abc"):
            self.assertFalse(by_name[name].conflict, name)
            self.assertEqual(by_name[name].status_text, "可整理")

    def test_conflict_count(self):
        # b.pdf 同名文件 + c.mp4 文件夹不可用
        self.assertEqual(self.plan.conflict_count, 2)

    def test_new_folders_list_not_created(self):
        names = {os.path.basename(p) for p in self.plan.new_folders}
        self.assertEqual(names, {"图片", "压缩包", "安装程序", "其他"})
        for path in self.plan.new_folders:
            self.assertFalse(os.path.exists(path), f"{path} 不应该被创建")

    def test_sorting_by_category(self):
        order = [i.category for i in self.plan.items]
        expected = sorted(order, key=lambda c: ["图片", "文档", "视频", "压缩包", "安装程序", "其他"].index(c))
        self.assertEqual(order, expected)

    def test_total_and_inside(self):
        # 6 个普通文件 + 那个占住「视频」名字的文件，它自己也算「其他」类要整理
        self.assertEqual(self.plan.total, 7)
        for item in self.plan.items:
            self.assertTrue(item.inside, item.name)

    def test_empty_input(self):
        plan = build_plan([], str(self.root))
        self.assertEqual(plan.total, 0)
        self.assertEqual(plan.conflict_count, 0)


class TestIsInside(unittest.TestCase):
    def test_inside(self):
        self.assertTrue(is_inside(r"D:\下载", r"D:\下载\文档\a.pdf"))

    def test_same_dir_is_not_inside(self):
        self.assertFalse(is_inside(r"D:\下载", r"D:\下载"))

    def test_outside(self):
        self.assertFalse(is_inside(r"D:\下载", r"D:\下载\..\别的地方\a.pdf"))

    def test_sibling_prefix_is_not_inside(self):
        # D:\下载2 不能被当成 D:\下载 的子目录
        self.assertFalse(is_inside(r"D:\下载", r"D:\下载2\a.pdf"))

    def test_case_insensitive_on_windows(self):
        self.assertTrue(is_inside(r"D:\下载", r"d:\下载\图片\a.jpg"))

    def test_different_drive(self):
        self.assertFalse(is_inside(r"C:\下载", r"D:\下载\a.pdf"))

    def test_empty_values(self):
        self.assertFalse(is_inside("", r"D:\a"))
        self.assertFalse(is_inside(r"D:\下载", ""))


if __name__ == "__main__":
    unittest.main()
