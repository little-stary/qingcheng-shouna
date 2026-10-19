# -*- coding: utf-8 -*-
"""打包自检函数的单元测试。

自检走的是完整链路（扫描 → 方案 → 整理 → 撤销），全部在临时目录里完成，
既验证打包产物，也顺便覆盖了各模块之间的协作。
"""

import unittest

from app.selftest import run


class TestSelftest(unittest.TestCase):
    def test_selftest_passes(self):
        ok, text = run()
        self.assertTrue(ok, "自检失败：\n" + text)
        self.assertIn("SELFTEST OK", text)


if __name__ == "__main__":
    unittest.main()
