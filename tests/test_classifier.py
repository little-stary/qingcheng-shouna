# -*- coding: utf-8 -*-
"""分类逻辑的单元测试。

运行方式（在项目目录下）：
    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

import unittest

from app.core.classifier import classify, get_ext


class TestGetExt(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(get_ext("报告.pdf"), ".pdf")

    def test_uppercase(self):
        self.assertEqual(get_ext("照片.JPG"), ".jpg")

    def test_no_ext(self):
        self.assertEqual(get_ext("README"), "")

    def test_dot_only(self):
        self.assertEqual(get_ext(".gitignore"), "")

    def test_multi_dot(self):
        self.assertEqual(get_ext("2025.10.18 备份.tar.gz"), ".gz")


class TestClassify(unittest.TestCase):
    def test_picture(self):
        for name in ("a.jpg", "b.PNG", "c.webp", "d.svg"):
            self.assertEqual(classify(name), "图片", name)

    def test_document(self):
        for name in ("论文.docx", "表格.xlsx", "说明.pdf", "笔记.txt"):
            self.assertEqual(classify(name), "文档", name)

    def test_video(self):
        for name in ("v.mp4", "v.mkv", "v.MOV"):
            self.assertEqual(classify(name), "视频", name)

    def test_archive(self):
        for name in ("z.zip", "z.7z", "z.tar.gz"):
            self.assertEqual(classify(name), "压缩包", name)

    def test_installer(self):
        for name in ("setup.exe", "app.msi", "win.iso"):
            self.assertEqual(classify(name), "安装程序", name)

    def test_other(self):
        for name in ("a.psd", "b.py", "无扩展名", "c.xyz"):
            self.assertEqual(classify(name), "其他", name)


if __name__ == "__main__":
    unittest.main()
