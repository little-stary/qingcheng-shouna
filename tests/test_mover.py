# -*- coding: utf-8 -*-
"""移动执行器的测试：覆盖需求里点名的 12 种情况。

测试全部在 tests/_tmp/ 下的临时目录里跑，不碰任何真实文件。
"""

import hashlib
import os
import shutil
import unittest
from pathlib import Path

from tests._cleanup import delete_path, remove_tree
from unittest import mock

from app.core.mover import (
    STATUS_FAILED,
    STATUS_MOVED,
    STATUS_SKIPPED,
    PlanItem,
    execute_plan,
    move_one,
)
from app.core.planner import Plan, build_plan
from app.core.records import RecordError, RecordStore
from app.core.scanner import scan_folder

TMP_ROOT = Path(__file__).parent / "_tmp" / "move_case"


def md5(path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


class MoveTestBase(unittest.TestCase):
    def setUp(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)
        self.root = TMP_ROOT / "下载"
        self.root.mkdir(parents=True)
        self.records = TMP_ROOT / "records.jsonl"

        (self.root / "照片.jpg").write_bytes(b"jpg-content")
        (self.root / "论文.pdf").write_bytes(b"pdf-content")
        (self.root / "电影.mp4").write_bytes(b"mp4-content")

    def tearDown(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)

    def make_plan(self) -> Plan:
        scan = scan_folder(str(self.root))
        return build_plan(scan.files, str(self.root))


class TestNormalMove(MoveTestBase):
    def test_normal_move(self):
        plan = self.make_plan()
        report = execute_plan(plan)

        self.assertEqual(report.moved, 3)
        self.assertEqual(report.failed, 0)
        self.assertEqual(report.skipped, 0)

        self.assertTrue((self.root / "图片" / "照片.jpg").exists())
        self.assertTrue((self.root / "文档" / "论文.pdf").exists())
        self.assertFalse((self.root / "照片.jpg").exists())

    def test_content_unchanged(self):
        before = md5(self.root / "论文.pdf")
        execute_plan(self.make_plan())
        self.assertEqual(md5(self.root / "文档" / "论文.pdf"), before)

    def test_chinese_filename(self):
        (self.root / "我的 2025 年总结（最终版）.docx").write_bytes(b"docx")
        plan = self.make_plan()
        execute_plan(plan)
        self.assertTrue((self.root / "文档" / "我的 2025 年总结（最终版）.docx").exists())

    def test_no_file_deleted(self):
        """整理后，文件总数不变（只是换了位置）。"""
        before = sum(len(files) for _, _, files in os.walk(self.root))
        execute_plan(self.make_plan())
        after = sum(len(files) for _, _, files in os.walk(self.root))
        self.assertEqual(before, after)


class TestConflicts(MoveTestBase):
    def test_same_name_conflict_skipped(self):
        # 目标里先放一个同名文件（模拟"之前整理过"）
        (self.root / "文档").mkdir()
        (self.root / "文档" / "论文.pdf").write_bytes(b"old")
        before_hash = md5(self.root / "文档" / "论文.pdf")

        report = execute_plan(self.make_plan())

        self.assertEqual(report.moved, 2)
        self.assertEqual(report.skipped, 1)
        skipped = [r for r in report.results if r.status == STATUS_SKIPPED][0]
        self.assertIn("同名", skipped.message)
        # 原有文件一个字节都不能变
        self.assertEqual(md5(self.root / "文档" / "论文.pdf"), before_hash)
        # 源文件也还在原地，没有被"跳过且删除"
        self.assertTrue((self.root / "论文.pdf").exists())

    def test_target_dir_name_taken_by_file(self):
        (self.root / "图片").write_bytes(b"i am a file")
        report = execute_plan(self.make_plan())

        failed = [r for r in report.results if r.status == STATUS_FAILED]
        self.assertEqual(len(failed), 1)
        self.assertIn("无法创建目标文件夹", failed[0].message)
        self.assertTrue((self.root / "照片.jpg").exists(), "失败时源文件必须原地不动")


class TestUnsafeCases(MoveTestBase):
    def test_source_disappeared_after_preview(self):
        plan = self.make_plan()
        delete_path(self.root / "论文.pdf")       # 预览之后被别的程序删了

        report = execute_plan(plan)

        skipped = [r for r in report.results if r.status == STATUS_SKIPPED]
        self.assertEqual(len(skipped), 1)
        self.assertIn("源文件已不存在", skipped[0].message)

    def test_outside_path_skipped(self):
        outside = TMP_ROOT / "别的地方"
        outside.mkdir(parents=True)
        item = PlanItem(
            src=str(self.root / "照片.jpg"),
            name="照片.jpg",
            category="图片",
            target_dir=str(outside),
            dst=str(outside / "照片.jpg"),
            inside=False,
        )
        plan = Plan(source_dir=str(self.root))
        plan.items.append(item)

        report = execute_plan(plan)

        self.assertEqual(report.skipped, 1)
        self.assertIn("不在所选目录内", report.results[0].message)
        self.assertFalse((outside / "照片.jpg").exists())

    def test_dst_outside_even_if_flag_says_inside(self):
        """防呆：即使 inside 标错了，也要按真实路径再算一次。"""
        outside = TMP_ROOT / "外面"
        outside.mkdir(parents=True)
        item = PlanItem(
            src=str(self.root / "照片.jpg"),
            name="照片.jpg",
            category="图片",
            target_dir=str(outside),
            dst=str(outside / "照片.jpg"),
            inside=True,              # 故意标成 True
        )
        result = move_one(item, str(self.root))
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertIn("不在所选目录内", result.message)


class TestFailures(MoveTestBase):
    def test_permission_error(self):
        with mock.patch("app.core.mover._move_file", side_effect=PermissionError("denied")):
            report = execute_plan(self.make_plan())

        self.assertEqual(report.failed, 3)
        self.assertIn("占用", report.results[0].message)
        self.assertTrue((self.root / "照片.jpg").exists())

    def test_disk_full(self):
        with mock.patch("app.core.mover._move_file",
                        side_effect=OSError(28, "No space left on device")):
            report = execute_plan(self.make_plan())
        self.assertEqual(report.failed, 3)
        self.assertIn("磁盘空间不足", report.results[0].message)

    def test_partial_success(self):
        """一个冲突、一个权限失败、一个成功。"""
        (self.root / "文档").mkdir()
        (self.root / "文档" / "论文.pdf").write_bytes(b"old")

        from app.core.mover import _move_file as real_move

        def flaky(src, dst):
            if "电影" in str(src):
                raise PermissionError("denied")
            return real_move(src, dst)

        with mock.patch("app.core.mover._move_file", side_effect=flaky):
            report = execute_plan(self.make_plan())

        self.assertEqual(report.moved, 1)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.failed, 1)


class TestStop(MoveTestBase):
    def test_stop_after_first(self):
        calls = {"n": 0}

        def should_cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 1          # 处理完第 1 个之后叫停

        report = execute_plan(self.make_plan(), should_cancel=should_cancel)

        self.assertTrue(report.stopped)
        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.remaining, 2)
        self.assertEqual(report.moved, 1)

    def test_stop_before_start(self):
        report = execute_plan(self.make_plan(), should_cancel=lambda: True)
        self.assertTrue(report.stopped)
        self.assertEqual(report.moved, 0)
        self.assertEqual(report.remaining, 3)


class TestRecords(MoveTestBase):
    def test_records_written_and_reloadable(self):
        store = RecordStore(self.records)
        report = execute_plan(self.make_plan(), store=store)

        # 换一个新的 Store 实例读取，模拟"程序重启后"
        reloaded = RecordStore(self.records).read_all()
        self.assertEqual(len(reloaded), 3)
        self.assertEqual({e["batch_id"] for e in reloaded}, {report.batch_id})
        self.assertTrue(all(e["status"] == STATUS_MOVED for e in reloaded))
        self.assertTrue(all(e["ts"] and e["src"] and e["dst"] for e in reloaded))

    def test_failed_not_recorded_as_moved(self):
        store = RecordStore(self.records)

        (self.root / "文档").mkdir()
        (self.root / "文档" / "论文.pdf").write_bytes(b"old")

        execute_plan(self.make_plan(), store=store)

        entries = RecordStore(self.records).read_all()
        statuses = sorted(e["status"] for e in entries)
        self.assertEqual(statuses, [STATUS_MOVED, STATUS_MOVED, STATUS_SKIPPED])
        self.assertNotIn(STATUS_FAILED, statuses)

    def test_record_failure_counted(self):
        """记录写不进去时，整理本身要继续，但必须计数以便提示用户。"""
        broken = RecordStore(TMP_ROOT / "不存在" / "x" / "records.jsonl")
        with mock.patch.object(broken, "append", side_effect=RecordError("磁盘只读")):
            report = execute_plan(self.make_plan(), store=broken)

        self.assertEqual(report.moved, 3, "记录失败不该影响移动")
        self.assertEqual(report.record_errors, 3)

    def test_batch_id_unique_per_run(self):
        store = RecordStore(self.records)
        r1 = execute_plan(self.make_plan(), store=store)
        r2 = execute_plan(self.make_plan(), store=store)
        self.assertNotEqual(r1.batch_id, r2.batch_id)


if __name__ == "__main__":
    unittest.main()
