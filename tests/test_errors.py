# -*- coding: utf-8 -*-
"""异常翻译的测试：保证用户看到的是中文人话，不是 Python 报错。"""

import unittest

from app.utils.errors import friendly_message


class TestFriendlyMessage(unittest.TestCase):
    def test_not_found(self):
        msg = friendly_message(FileNotFoundError("x"))
        self.assertIn("不存在", msg)

    def test_not_a_directory(self):
        msg = friendly_message(NotADirectoryError("x"))
        self.assertIn("不是文件夹", msg)

    def test_permission(self):
        msg = friendly_message(PermissionError("x"))
        self.assertIn("权限", msg)

    def test_no_space(self):
        msg = friendly_message(OSError("No space left on device"))
        self.assertIn("空间不足", msg)

    def test_unknown(self):
        msg = friendly_message(ValueError("whatever"))
        self.assertTrue(msg.endswith("。"))
        self.assertNotIn("Traceback", msg)

    def test_all_messages_are_chinese(self):
        for exc in (FileNotFoundError(), NotADirectoryError(), PermissionError(), OSError(), ValueError()):
            msg = friendly_message(exc)
            self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in msg), msg)


if __name__ == "__main__":
    unittest.main()
