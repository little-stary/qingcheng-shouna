# -*- coding: utf-8 -*-
"""安全审查专项测试（第四阶段完成后补的一轮）。

覆盖的问题：
    1. 方案被伪造时，源文件可能来自所选目录之外；
    2. 源文件在子文件夹里（只处理第一层）；
    3. 目录联接（mklink /J）把目标指向目录外面；
    4. 源文件是符号链接；
    5. 记录文件上次写到一半断电，新记录会不会跟着坏；
    6. 「准备移动 / 移动结果」两行合并成一条；
    7. 断电后只剩「准备移动」，能不能看出文件去哪了；
    8. 移动成功但校验异常时，不能记成"失败"；
    9. 停止后不再碰文件，记录也只到最后处理的那个；
   10. 方案里的相对路径必须转成绝对路径（否则撤销会失效）。

全部在 tests/_tmp/security_case 下自建目录，结束即删，不碰真实文件。
"""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests._cleanup import remove_tree
from unittest import mock

from app.core.models import FileItem
from app.core.mover import (
    STATUS_MOVED,
    STATUS_SKIPPED,
    PlanItem,
    execute_plan,
    move_one,
)
from app.core.planner import Plan, build_plan
from app.core.records import RecordError, RecordStore, default_record_path
from app.utils.paths import is_inside

TMP_ROOT = Path(__file__).parent / "_tmp" / "security_case"


class SecurityTestBase(unittest.TestCase):
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

    def make_item(self, name: str, dst: Path, category: str = "图片",
                  src: Path = None, inside: bool = True) -> PlanItem:
        return PlanItem(
            src=str(src if src is not None else self.root / name),
            name=name,
            category=category,
            target_dir=str(dst.parent),
            dst=str(dst),
            inside=inside,
        )


class TestPathBoundary(SecurityTestBase):
    def test_source_outside_selected_dir_is_skipped(self):
        """伪造的方案：src 指到所选目录之外，必须拒绝。"""
        outside = TMP_ROOT / "别处"
        outside.mkdir()
        victim = outside / "重要资料.docx"
        victim.write_bytes(b"must-not-move")

        item = self.make_item(
            "重要资料.docx", self.root / "文档" / "重要资料.docx",
            category="文档", src=victim,
        )
        plan = Plan(source_dir=str(self.root))
        plan.items.append(item)

        report = execute_plan(plan)

        self.assertEqual(report.moved, 0)
        self.assertEqual(report.skipped, 1)
        self.assertIn("源文件不在所选目录内", report.results[0].message)
        self.assertTrue(victim.exists(), "目录外的文件不能被移走")

    def test_source_in_subfolder_is_skipped(self):
        """只整理第一层：子文件夹里的文件不该被搬。"""
        self.make_file("子文件夹/深层.jpg", b"deep")
        item = self.make_item(
            "深层.jpg", self.root / "图片" / "深层.jpg",
            src=self.root / "子文件夹" / "深层.jpg",
        )
        plan = Plan(source_dir=str(self.root))
        plan.items.append(item)

        report = execute_plan(plan)

        self.assertEqual(report.skipped, 1)
        self.assertIn("第一层", report.results[0].message)
        self.assertTrue((self.root / "子文件夹" / "深层.jpg").exists())

    def test_bad_filename_with_separator_is_skipped(self):
        """文件名里带路径分隔符，可能把目标拼到别的目录去。"""
        self.make_file("照片.jpg")
        item = PlanItem(
            src=str(self.root / "照片.jpg"),
            name="..\\逃出去.jpg",
            category="图片",
            target_dir=str(self.root / "图片"),
            dst=str(self.root / "图片" / "照片.jpg"),
        )
        result = move_one(item, str(self.root))
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertIn("文件名不合法", result.message)

    def test_dotdot_traversal_rejected(self):
        self.assertFalse(is_inside(str(self.root), str(self.root / ".." / "逃出去.txt")))

    def test_junction_escape_rejected(self):
        """目录联接（mklink /J）指向外面时，realpath 必须识破。"""
        outside = TMP_ROOT / "外部目标"
        outside.mkdir()
        link = self.root / "图片"

        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
        )
        detail = (created.stderr or created.stdout).decode("gbk", errors="replace")
        if created.returncode != 0 or not os.path.isdir(link):
            self.skipTest(f"当前系统无法创建目录联接：{detail.strip()}")

        self.make_file("照片.jpg")
        item = self.make_item("照片.jpg", link / "照片.jpg")

        report = execute_plan_from(item, self.root)

        self.assertEqual(report.moved, 0)
        self.assertIn("不在所选目录内", report.results[0].message)
        self.assertFalse((outside / "照片.jpg").exists(), "文件不能被搬到联接指向的位置")

    def test_symlink_source_rejected(self):
        """真实环境里创建一个符号链接来验证（无权限时自动跳过）。"""
        target = TMP_ROOT / "真身.bin"
        target.write_bytes(b"real")
        link = self.root / "快捷.jpg"
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前系统不允许创建符号链接：{exc}")
        if not os.path.islink(link):
            self.skipTest("当前环境未真正创建出符号链接（系统策略拦截）")

        item = self.make_item("快捷.jpg", self.root / "图片" / "快捷.jpg")
        report = execute_plan_from(item, self.root)

        self.assertEqual(report.moved, 0)
        self.assertEqual(report.skipped, 1)
        self.assertTrue(target.exists(), "链接指向的真实文件不能被移动")

    def test_symlink_source_rejected_logic(self):
        """不依赖系统权限：直接把 src 判成链接，验证代码会拒绝。"""
        self.make_file("快捷.jpg")
        item = self.make_item("快捷.jpg", self.root / "图片" / "快捷.jpg")

        real_islink = os.path.islink

        def fake_islink(path):
            if str(path).endswith("快捷.jpg"):
                return True
            return real_islink(path)

        with mock.patch("app.core.mover.os.path.islink", side_effect=fake_islink):
            result = move_one(item, str(self.root))

        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertIn("链接", result.message)
        self.assertTrue((self.root / "快捷.jpg").exists())


class TestRecordDurability(SecurityTestBase):
    def test_half_written_line_does_not_swallow_next_record(self):
        """上次写到一半断电，文件末尾没有换行：新记录必须还读得出来。"""
        self.records.write_text('{"batch_id": "old", "src": "a"}', encoding="utf-8")
        store = RecordStore(self.records)

        store.append({"batch_id": "new", "src": "b"})

        entries = store.read_all()
        self.assertEqual([e["batch_id"] for e in entries], ["old", "new"])
        self.assertEqual(entries[1]["src"], "b")

    def test_begin_and_end_collapse_into_one(self):
        store = RecordStore(self.records)
        store.append({"batch_id": "b1", "phase": "begin", "status": "moving",
                      "src": "a.jpg", "dst": "图片\\a.jpg"})
        store.append({"batch_id": "b1", "phase": "end", "status": STATUS_MOVED,
                      "src": "a.jpg", "dst": "图片\\a.jpg"})

        entries = store.read_all()
        self.assertEqual(len(entries), 1, "一个文件应该只有一条最终记录")
        self.assertEqual(entries[0]["status"], STATUS_MOVED)

    def test_interrupted_move_is_visible_as_moving(self):
        """只有 begin 没有 end：说明上次运行中途断了，撤销时要按它去核实。"""
        store = RecordStore(self.records)
        store.append({"batch_id": "b2", "phase": "begin", "status": "moving",
                      "src": "a.jpg", "dst": "图片\\a.jpg"})

        entries = RecordStore(self.records).read_all()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "moving")

    def test_default_record_path_is_outside_project(self):
        project = Path(__file__).resolve().parents[1]
        record = str(default_record_path()).lower()
        self.assertFalse(
            record.startswith(str(project).lower()),
            "记录里有个人文件路径，不能放在项目目录里（会被提交到 GitHub）",
        )
        self.assertNotIn("qingcheng-shouna", record)

    def test_corrupt_lines_are_counted(self):
        store = RecordStore(self.records)
        store.append({"batch_id": "ok", "src": "a"})
        with open(self.records, "a", encoding="utf-8") as f:
            f.write("{坏行\n")
        store.append({"batch_id": "ok2", "src": "b"})

        entries = store.read_all()
        self.assertEqual(len(entries), 2)
        self.assertEqual(store.skipped_lines, 1, "坏行要能被统计出来，方便以后提示用户")


class TestMoveSafety(SecurityTestBase):
    def test_verification_failure_is_still_recorded_as_moved(self):
        """文件已经搬走了，就不能记成"失败"——否则用户以为它还在原地。"""
        self.make_file("照片.jpg", b"abc")
        item = self.make_item("照片.jpg", self.root / "图片" / "照片.jpg")

        with mock.patch("app.core.mover.os.path.getsize", side_effect=[3, 999]):
            result = move_one(item, str(self.root))

        self.assertEqual(result.status, STATUS_MOVED)
        self.assertFalse(result.verified)
        self.assertIn("大小", result.message)
        self.assertTrue((self.root / "图片" / "照片.jpg").exists())

    def test_race_created_target_is_skipped_not_failed(self):
        """检查之后、移动之前被别人抢建同名文件：跳过，不覆盖。"""
        self.make_file("照片.jpg")
        item = self.make_item("照片.jpg", self.root / "图片" / "照片.jpg")

        with mock.patch("app.core.mover._move_file", side_effect=FileExistsError("exists")):
            result = move_one(item, str(self.root))

        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertIn("同名", result.message)
        self.assertTrue((self.root / "照片.jpg").exists(), "源文件必须原地不动")

    def test_special_characters_in_name(self):
        name = "报 告 #1 & 汇总 (终版) [2025].pdf"
        self.make_file(name)
        item = self.make_item(name, self.root / "文档" / name, category="文档")

        result = move_one(item, str(self.root))

        self.assertEqual(result.status, STATUS_MOVED)
        self.assertTrue((self.root / "文档" / name).exists())

    def test_progress_callback_error_does_not_break_batch(self):
        self.make_file("a.jpg")
        plan = Plan(source_dir=str(self.root))
        plan.items.append(self.make_item("a.jpg", self.root / "图片" / "a.jpg"))

        def boom(done, total):
            raise RuntimeError("界面回调炸了")

        report = execute_plan(plan, on_progress=boom)
        self.assertEqual(report.moved, 1, "界面回调出错不能影响文件移动")

    def test_record_errors_counted_per_file(self):
        self.make_file("a.jpg")
        self.make_file("b.pdf")
        plan = build_plan(
            [
                FileItem(str(self.root / "a.jpg"), "a.jpg", ".jpg", 1, 0.0, "图片"),
                FileItem(str(self.root / "b.pdf"), "b.pdf", ".pdf", 1, 0.0, "文档"),
            ],
            str(self.root),
        )

        broken = RecordStore(self.records)
        with mock.patch.object(broken, "append", side_effect=RecordError("磁盘只读")):
            report = execute_plan(plan, store=broken)

        self.assertEqual(report.moved, 2, "记录失败不该影响移动")
        self.assertEqual(report.record_errors, 2, "按文件计，一个文件算一次")


class TestStopAccounting(SecurityTestBase):
    def test_stop_leaves_rest_untouched_and_unrecorded(self):
        for name in ("a.jpg", "b.jpg", "c.jpg"):
            self.make_file(name)

        plan = build_plan(
            [
                FileItem(str(self.root / n), n, ".jpg", 1, 0.0, "图片")
                for n in ("a.jpg", "b.jpg", "c.jpg")
            ],
            str(self.root),
        )

        calls = {"n": 0}

        def should_cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 1           # 处理完第 1 个就叫停

        store = RecordStore(self.records)
        report = execute_plan(plan, should_cancel=should_cancel, store=store)

        self.assertTrue(report.stopped)
        self.assertEqual(report.moved, 1)
        self.assertEqual(report.remaining, 2)
        self.assertEqual(len(store.read_batch(report.batch_id)), 1,
                         "没处理的文件不该留下记录")

        remaining = [n for n in ("b.jpg", "c.jpg") if (self.root / n).exists()]
        self.assertEqual(len(remaining), 2, "停止之后不能再动文件")


class TestPlanPaths(SecurityTestBase):
    def test_relative_source_becomes_absolute(self):
        """扫描传相对路径时，方案里也必须是绝对路径，否则撤销会找不到文件。"""
        f = FileItem("相对.txt", "相对.txt", ".txt", 1, 0.0, "文档")
        plan = build_plan([f], str(self.root))
        self.assertTrue(os.path.isabs(plan.items[0].src))
        self.assertTrue(os.path.isabs(plan.items[0].dst))

    def test_partial_batch_records_match_results(self):
        self.make_file("a.jpg")
        self.make_file("b.pdf")
        (self.root / "文档").mkdir()
        (self.root / "文档" / "b.pdf").write_bytes(b"old")

        plan = build_plan(
            [
                FileItem(str(self.root / "a.jpg"), "a.jpg", ".jpg", 1, 0.0, "图片"),
                FileItem(str(self.root / "b.pdf"), "b.pdf", ".pdf", 1, 0.0, "文档"),
            ],
            str(self.root),
        )

        store = RecordStore(self.records)
        report = execute_plan(plan, store=store)
        entries = store.read_batch(report.batch_id)

        self.assertEqual(len(entries), 2)
        self.assertEqual(sorted(e["status"] for e in entries), [STATUS_MOVED, STATUS_SKIPPED])
        for e in entries:
            self.assertTrue(e["ts"] and e["src"] and e["dst"] and e["batch_id"])
        # 跳过的那个必须指向真实的目标路径，撤销时要靠它判断
        skipped = [e for e in entries if e["status"] == STATUS_SKIPPED][0]
        self.assertTrue(skipped["dst"].endswith("b.pdf"))


def execute_plan_from(item: PlanItem, root: Path):
    """把单个方案项包成 Plan 跑一遍，省得每个用例都写三行。"""
    plan = Plan(source_dir=str(root))
    plan.items.append(item)
    return execute_plan(plan)


if __name__ == "__main__":
    unittest.main()
