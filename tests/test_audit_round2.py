# -*- coding: utf-8 -*-
"""第二轮专项审查的测试：逐条对应提出的 5 个问题。

Q1 移动成功但记录保存失败    → TestRecordFailureRecovery
Q2 点击停止后是否还继续移动  → TestStopSemantics
Q3 移动前是否重新验证        → TestRevalidationBeforeMove
Q4 异常退出时状态与记录一致性 → TestCrashConsistency
Q5 目标是否可能被覆盖        → TestNoOverwrite

所有目录都建在 tests/_tmp/audit2 下，用完即删，不碰任何真实文件。
"""

import os
import shutil
import unittest
from pathlib import Path

from tests._cleanup import remove_tree
from unittest import mock

from app.core.models import FileItem
from app.core.mover import (
    STATUS_MOVED,
    STATUS_SKIPPED,
    PlanItem,
    _move_file,
    execute_plan,
    move_one,
)
from app.core.planner import build_plan
from app.core.records import RecordError, RecordStore

TMP_ROOT = Path(__file__).parent / "_tmp" / "audit2"


class AuditBase(unittest.TestCase):
    def setUp(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)
        self.root = TMP_ROOT / "下载"
        self.root.mkdir(parents=True)
        self.records = TMP_ROOT / "records.jsonl"

    def tearDown(self):
        if TMP_ROOT.exists():
            remove_tree(TMP_ROOT)

    def make_file(self, name: str, data: bytes = b"x") -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def plan_for(self, items):
        """items: [(文件名, 分类), ...] → 与界面完全一致的 Plan。"""
        files = [
            FileItem(str(self.root / name), name, os.path.splitext(name)[1].lower(), 1, 0.0, cat)
            for name, cat in items
        ]
        return build_plan(files, str(self.root))

    def item(self, name, dst, category="图片", src=None):
        return PlanItem(
            src=str(src if src is not None else self.root / name),
            name=name,
            category=category,
            target_dir=str(Path(dst).parent),
            dst=str(dst),
        )


# --------------------------------------------------------------------- Q1
class FlakyStore(RecordStore):
    """指定的第 N 次写入失败，其余正常（模拟磁盘被短暂占用）。"""

    def __init__(self, path, fail_calls):
        super().__init__(path)
        self.fail_calls = set(fail_calls)
        self.calls = 0

    def append(self, entry):
        self.calls += 1
        if self.calls in self.fail_calls:
            raise RecordError("模拟瞬时写入失败")
        return super().append(entry)


class BeginFailStore(RecordStore):
    """只有"准备移动"写不进去，"结果"能正常写。"""

    def append(self, entry):
        if entry.get("phase") == "begin":
            raise RecordError("模拟 begin 写入失败")
        return super().append(entry)


class EndFailStore(RecordStore):
    """只有"结果"写不进去（磁盘满、文件被占用）。"""

    def append(self, entry):
        if entry.get("phase") == "end":
            raise RecordError("模拟 end 写入失败")
        return super().append(entry)


class TestRecordFailureRecovery(AuditBase):
    def test_transient_failure_is_retried(self):
        """结果记录第一次写失败、收尾重试成功 → 不应报"记录不完整"。"""
        self.make_file("a.jpg")
        self.make_file("b.jpg")
        plan = self.plan_for([("a.jpg", "图片"), ("b.jpg", "图片")])

        store = FlakyStore(self.records, fail_calls={2})   # 第 2 次 = a 的结果
        report = execute_plan(plan, store=store)

        self.assertEqual(report.moved, 2)
        self.assertEqual(report.record_errors, 0, "重试成功后不该再报错")
        self.assertEqual(report.record_failed_items, [])

        entries = RecordStore(self.records).read_all()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["status"] == STATUS_MOVED for e in entries),
                        "重试写入的必须是最终结果，不能是 moving")

    def test_begin_failure_does_not_break_result_record(self):
        """begin 写不进去，但 end 写成功 → 记录仍然完整。"""
        self.make_file("a.jpg")
        plan = self.plan_for([("a.jpg", "图片")])

        report = execute_plan(plan, store=BeginFailStore(self.records))

        self.assertEqual(report.moved, 1)
        self.assertEqual(report.record_errors, 0)
        entries = RecordStore(self.records).read_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], STATUS_MOVED)

    def test_permanent_failure_lists_the_files(self):
        """两条都写不进去 → 必须报出数量，还要报出是哪些文件。"""
        self.make_file("a.jpg")
        self.make_file("b.jpg")
        plan = self.plan_for([("a.jpg", "图片"), ("b.jpg", "图片")])

        report = execute_plan(plan, store=EndFailStore(self.records))

        self.assertEqual(report.moved, 2, "记录失败不能影响移动")
        self.assertEqual(report.record_errors, 2)
        self.assertEqual(
            report.record_failed_items,
            sorted([str(self.root / "a.jpg"), str(self.root / "b.jpg")]),
            "必须能告诉用户是哪几个文件没记录",
        )

        # 记录里只剩 begin，状态是 moving —— 撤销可以据此去磁盘核实
        entries = RecordStore(self.records).read_all()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["status"] == "moving" for e in entries))

        # 文件确实已经搬走：说明"状态与记录不一致"是能被检测出来的
        self.assertTrue((self.root / "图片" / "a.jpg").exists())


# --------------------------------------------------------------------- Q2
class TestStopSemantics(AuditBase):
    def test_stop_from_progress_stops_remaining(self):
        """界面点停止时，正好是 on_progress 之后 —— 复现真实时序。"""
        for n in ("a.jpg", "b.jpg", "c.jpg"):
            self.make_file(n)
        plan = self.plan_for([(n, "图片") for n in ("a.jpg", "b.jpg", "c.jpg")])

        state = {"stop": False}

        def on_progress(done, total):
            if done == 1:
                state["stop"] = True          # 用户点了「停止整理」

        store = RecordStore(self.records)
        report = execute_plan(
            plan, on_progress=on_progress, should_cancel=lambda: state["stop"], store=store
        )

        self.assertTrue(report.stopped)
        self.assertEqual(report.moved, 1, "停止后不能再移动第二个文件")
        self.assertEqual(report.remaining, 2)

        still_there = [n for n in ("b.jpg", "c.jpg") if (self.root / n).exists()]
        self.assertEqual(len(still_there), 2, "未处理的文件必须原地不动")
        self.assertFalse((self.root / "图片" / "b.jpg").exists())

        self.assertEqual(len(store.read_batch(report.batch_id)), 1,
                         "未处理的文件不该留下任何记录")

    def test_stop_before_start_moves_nothing(self):
        self.make_file("a.jpg")
        plan = self.plan_for([("a.jpg", "图片")])
        store = RecordStore(self.records)

        report = execute_plan(plan, should_cancel=lambda: True, store=store)

        self.assertTrue(report.stopped)
        self.assertEqual(report.moved, 0)
        self.assertEqual(report.remaining, 1)
        self.assertEqual(store.read_batch(report.batch_id), [])
        self.assertFalse((self.root / "图片").exists(), "停止后不该创建分类文件夹")


# --------------------------------------------------------------------- Q3
class TestRevalidationBeforeMove(AuditBase):
    def test_each_check_blocks_before_move(self):
        """五种不合法情况，都必须在不调用移动原语的前提下被拦下。"""
        self.make_file("照片.jpg")
        (self.root / "子文件夹").mkdir()
        self.make_file("子文件夹/深层.jpg")

        outside = TMP_ROOT / "外面"
        outside.mkdir()

        cases = [
            ("目标越界", self.item("照片.jpg", outside / "照片.jpg"), "不在所选目录内"),
            ("源在第一层之外",
             self.item("深层.jpg", self.root / "图片" / "深层.jpg",
                       src=self.root / "子文件夹" / "深层.jpg"), "第一层"),
            ("文件名含分隔符",
             self.item("..\\逃.jpg", self.root / "图片" / "照片.jpg",
                       src=self.root / "照片.jpg"), "文件名不合法"),
            ("源是目录",
             self.item("子文件夹", self.root / "其他" / "子文件夹"), "不是普通文件"),
            ("源不存在",
             self.item("不存在.jpg", self.root / "图片" / "不存在.jpg"), "源文件已不存在"),
        ]

        for label, item, expected in cases:
            with self.subTest(label), \
                    mock.patch("app.core.mover._move_file") as move_mock:
                result = move_one(item, str(self.root))

                self.assertEqual(result.status, STATUS_SKIPPED, label)
                self.assertIn(expected, result.message, label)
                move_mock.assert_not_called()

        self.assertTrue((self.root / "照片.jpg").exists())
        self.assertFalse((outside / "照片.jpg").exists())

    def test_dangling_entry_at_target_is_detected(self):
        """悬空链接在 exists 眼里是"不存在"，但它确实占了名字，必须跳过。"""
        self.make_file("照片.jpg")
        item = self.item("照片.jpg", self.root / "图片" / "照片.jpg")

        real_lexists = os.path.lexists

        def fake_lexists(path):
            if os.path.normcase(str(path)) == os.path.normcase(item.dst):
                return True                      # 模拟一个断掉的链接
            return real_lexists(path)

        with mock.patch("app.core.mover.os.path.lexists", side_effect=fake_lexists), \
                mock.patch("app.core.mover._move_file") as move_mock:
            result = move_one(item, str(self.root))

        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertIn("同名", result.message)
        move_mock.assert_not_called()


# --------------------------------------------------------------------- Q4
class FsCheckingStore(RecordStore):
    """在写记录的那一刻检查磁盘状态，用来证明写入顺序。"""

    def __init__(self, path, src, dst):
        super().__init__(path)
        self.src, self.dst = src, dst
        self.checks = []

    def append(self, entry):
        self.checks.append((entry.get("phase"),
                            os.path.exists(self.src),
                            os.path.exists(self.dst)))
        return super().append(entry)


class CrashOnEndStore(RecordStore):
    """模拟程序在"移动完成、结果记录还没写"时被强行结束。"""

    def append(self, entry):
        if entry.get("phase") == "end":
            raise KeyboardInterrupt("模拟断电/被杀进程")
        return super().append(entry)


class TestCrashConsistency(AuditBase):
    def test_begin_is_written_before_move(self):
        """begin 落盘时源文件还在、目标还不存在；end 落盘时反过来。"""
        src = self.make_file("照片.jpg")
        dst = self.root / "图片" / "照片.jpg"
        plan = self.plan_for([("照片.jpg", "图片")])

        store = FsCheckingStore(self.records, str(src), str(dst))
        execute_plan(plan, store=store)

        self.assertEqual(store.checks[0], ("begin", True, False),
                         "必须先写记录再移动，否则断电后无法知道文件去哪了")
        self.assertEqual(store.checks[1], ("end", False, True))

    def test_crash_after_move_is_detectable(self):
        """移动后立刻崩溃：记录里是 moving，文件已经在目标位置——可核对。"""
        self.make_file("照片.jpg")
        plan = self.plan_for([("照片.jpg", "图片")])

        with self.assertRaises(KeyboardInterrupt):
            execute_plan(plan, store=CrashOnEndStore(self.records))

        entries = RecordStore(self.records).read_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "moving",
                         "只剩 begin 的记录必须保留下来，供撤销时核实")
        self.assertTrue((self.root / "图片" / "照片.jpg").exists())
        self.assertFalse((self.root / "照片.jpg").exists())


# --------------------------------------------------------------------- Q5
class TestNoOverwrite(AuditBase):
    def test_primitive_refuses_existing_file(self):
        src = self.make_file("新文件.txt", b"NEW")
        dst = self.root / "旧文件.txt"
        dst.write_bytes(b"OLD")

        with self.assertRaises(FileExistsError):
            _move_file(str(src), str(dst))

        self.assertEqual(dst.read_bytes(), b"OLD", "目标内容一个字节都不能变")
        self.assertEqual(src.read_bytes(), b"NEW", "源文件必须还在")
        self.assertTrue(src.exists())

    def test_primitive_refuses_existing_directory(self):
        src = self.make_file("照片.jpg")
        target_dir = self.root / "图片"
        target_dir.mkdir()
        (target_dir / "已有.jpg").write_bytes(b"KEEP")

        with self.assertRaises(OSError):
            _move_file(str(src), str(target_dir))

        self.assertEqual((target_dir / "已有.jpg").read_bytes(), b"KEEP")
        self.assertTrue(src.exists(), "失败时源文件不能被移走")

    def test_conflict_file_content_unchanged_after_batch(self):
        """整批跑完，冲突文件内容与源文件都保持原样。"""
        self.make_file("a.jpg", b"AAA")
        self.make_file("b.pdf", b"BBB")
        (self.root / "文档").mkdir()
        (self.root / "文档" / "b.pdf").write_bytes(b"OLD-CONTENT")

        report = execute_plan(self.plan_for([("a.jpg", "图片"), ("b.pdf", "文档")]))

        self.assertEqual(report.moved, 1)
        self.assertEqual(report.skipped, 1)
        self.assertEqual((self.root / "文档" / "b.pdf").read_bytes(), b"OLD-CONTENT")
        self.assertEqual((self.root / "b.pdf").read_bytes(), b"BBB")
        self.assertEqual((self.root / "图片" / "a.jpg").read_bytes(), b"AAA")


# --------------------------------------------------------------------- 静态检查
class TestUiImports(unittest.TestCase):
    """防止"用了模块却没导入"这类只能靠点界面才发现的错误。

    上一轮在 main_window.py 里加了 os.path.abspath(...) 却忘了 import os，
    结果一点「选择文件夹」就 NameError —— 单元测试全绿也发现不了。
    """

    STDLIB_MODULES = {
        "os", "sys", "json", "re", "shutil", "threading", "time",
        "uuid", "subprocess", "datetime", "ctypes", "pathlib",
    }

    def test_all_modules_are_imported_before_use(self):
        import ast

        app_dir = Path(__file__).resolve().parent.parent / "app"
        problems = []

        for path in app_dir.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add((alias.asname or alias.name).split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        imported.add(alias.asname or alias.name)

            used = {
                node.value.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            }

            missing = (used & self.STDLIB_MODULES) - imported
            if missing:
                problems.append(f"{path.relative_to(app_dir)}: {sorted(missing)}")

        self.assertEqual(problems, [], f"使用了未导入的模块，运行时会 NameError：{problems}")


if __name__ == "__main__":
    unittest.main()
