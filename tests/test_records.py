# -*- coding: utf-8 -*-
"""操作记录的测试：写入、重启后读取、坏行容忍、写失败要报错。"""

import json
import shutil
import unittest
from pathlib import Path

from tests._cleanup import remove_tree

from app.core.records import RecordError, RecordStore

TMP_ROOT = Path(__file__).parent / "_tmp" / "record_case"


class TestRecordStore(unittest.TestCase):
    def setUp(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)
        TMP_ROOT.mkdir(parents=True)
        self.path = TMP_ROOT / "records.jsonl"
        self.store = RecordStore(self.path)

    def tearDown(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)

    def test_missing_file_returns_empty(self):
        self.assertEqual(self.store.read_all(), [])

    def test_append_and_read(self):
        self.store.append({"ts": "2026-10-18 20:00:00", "batch_id": "abc",
                           "status": "moved", "src": r"D:\a\中文.pdf", "dst": r"D:\a\文档\中文.pdf"})
        entries = self.store.read_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["src"], r"D:\a\中文.pdf")

    def test_chinese_not_escaped(self):
        self.store.append({"src": "D:\\下载\\报告.pdf"})
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("报告", raw, "中文应原样保存，便于人工查看")
        self.assertNotIn("\\u", raw)

    def test_restart_can_read(self):
        self.store.append({"batch_id": "b1", "status": "moved"})
        again = RecordStore(self.path)          # 模拟程序重启
        self.assertEqual(len(again.read_all()), 1)

    def test_read_batch(self):
        self.store.append({"batch_id": "b1", "src": "a"})
        self.store.append({"batch_id": "b2", "src": "b"})
        self.store.append({"batch_id": "b1", "src": "c"})
        self.assertEqual(len(self.store.read_batch("b1")), 2)

    def test_broken_line_ignored(self):
        self.store.append({"batch_id": "ok"})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("{这一行是坏的\n")            # 模拟写到一半断电
        self.store.append({"batch_id": "ok2"})
        entries = self.store.read_all()
        self.assertEqual([e["batch_id"] for e in entries], ["ok", "ok2"])

    def test_write_failure_raises(self):
        # 把一个普通文件当成目录用，mkdir 必然失败
        blocker = TMP_ROOT / "blocker"
        blocker.write_text("x", encoding="utf-8")
        bad = RecordStore(blocker / "records.jsonl")
        with self.assertRaises(RecordError):
            bad.append({"batch_id": "x"})

    def test_entries_are_valid_json_per_line(self):
        for i in range(3):
            self.store.append({"i": i})
        lines = self.path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 3)
        for line in lines:
            json.loads(line)                     # 每行都必须是合法 JSON


if __name__ == "__main__":
    unittest.main()
