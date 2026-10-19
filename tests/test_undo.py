# -*- coding: utf-8 -*-
"""撤销功能的测试：覆盖「撤销本次整理」上线前要验的 12 类情况。

测试原则（和 test_mover.py 保持一致）：
    1. 所有文件操作都发生在 tests/_tmp/undo_case/<用例名>/ 下，
       每个用例自己建、自己删，绝不碰 tests/_tmp 里的其它内容，更不碰用户真实文件；
    2. 每条断言都要落到**磁盘真实状态**上（文件在不在、内容 md5、有没有残留），
       不能只看返回值；
    3. 造批次用真实链路（scan_folder → build_plan → execute_plan），
       记录格式与程序实际运行时完全一致，避免"测试造的数据源本身是假的"。

一个必须说清的统计口径（测试里反复用到）：
    · UndoPlan.skipped_count —— 生成方案时就已经判定"不动"的条目（不进入执行）；
    · UndoReport.skipped     —— 真正进入执行阶段、动手时才发现不能搬的条目。
    方案阶段跳过的东西根本没被碰过，所以不会出现在 report.results 里。
    界面会把两边的数字加起来展示（见 app/ui/history.py:_on_undo_done 里的
    pre_skipped），测试里也对应用"plan.skipped_count + report.skipped"核对总数。

不测 UI（Tk 需要真实 mainloop，容易 flaky），只做 import 检查。
"""

import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path

from tests._cleanup import delete_path, remove_tree

from app.core.mover import execute_plan
from app.core.planner import build_plan
from app.core.records import RecordError, RecordStore, now_text
from app.core.scanner import scan_folder
from app.core.undoer import (
    PHASE_UNDO,
    REASON_ALREADY_UNDONE,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_RESTORED,
    STATUS_SKIPPED,
    BatchSummary,
    UndoItem,
    UndoPlan,
    UndoReport,
    build_undo_plan,
    execute_undo,
    list_batches,
)

TMP_ROOT = Path(__file__).parent / "_tmp" / "undo_case"


# ---------------------------------------------------------------------- 小工具
def md5(path) -> str:
    """文件内容指纹：判断"文件是不是原封不动"的最终依据。"""
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def top_files(folder) -> list:
    """只列第一层的文件名（整理目录的第一层就该是"待整理的文件"）。"""
    base = Path(folder)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_file())


def all_files(folder) -> list:
    """递归列出目录下所有文件的相对路径（用于确认"没有残留"）。"""
    base = Path(folder)
    if not base.is_dir():
        return []
    return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())


class UndoTempCase(unittest.TestCase):
    """所有撤销用例的公共骨架：每个用例一个独立目录，互不影响。"""

    #: 文件名 -> 内容，子类可覆盖
    FILES = {
        "照片.jpg": b"jpg-content-0123456789",
        "论文.pdf": b"pdf-content-0123456789",
        "电影.mp4": b"mp4-content-0123456789",
        "工具.zip": b"zip-content-0123456789",
    }

    def setUp(self):
        self.case_dir = TMP_ROOT / f"{type(self).__name__}_{self._testMethodName}"
        if self.case_dir.exists():
            remove_tree(self.case_dir)
        self.root = self.case_dir / "下载"
        self.root.mkdir(parents=True)
        self.records_path = self.case_dir / "records.jsonl"
        self.store = RecordStore(self.records_path)

    def tearDown(self):
        if self.case_dir.exists():
            remove_tree(self.case_dir)

    # ------------------------------------------------------------- 造数据
    def expected_md5(self, names=None) -> dict:
        """算出这批文件"整理前"应有的内容指纹（不落盘，纯计算）。"""
        names = list(self.FILES) if names is None else list(names)
        return {name: hashlib.md5(self.FILES[name]).hexdigest() for name in names}

    def write_files(self, names=None) -> dict:
        """在整理目录第一层写出文件，返回 {文件名: md5}。"""
        names = list(self.FILES) if names is None else list(names)
        for name in names:
            (self.root / name).write_bytes(self.FILES[name])
        return self.expected_md5(names)

    def run_organize(self, store=None, expect=None):
        """对目录里现有的文件走一遍真实整理链路（不新建文件）。"""
        store = self.store if store is None else store
        scan = scan_folder(str(self.root))
        plan = build_plan(scan.files, str(self.root))
        report = execute_plan(plan, store=store)
        if expect is not None:
            self.assertEqual(
                report.moved, expect,
                f"造数据阶段就失败了：预期移动 {expect} 个，实际 {report.moved} 个",
            )
        return report

    def organize(self, names=None, store=None):
        """写出文件 + 整理一次，返回 MoveReport。"""
        self.write_files(names)
        expected = len(self.FILES) if names is None else len(names)
        return self.run_organize(store=store, expect=expected)

    def undo(self, batch_id, store=None, **kwargs):
        """生成方案并执行撤销，返回 (plan, report)。"""
        store = self.store if store is None else store
        plan = build_undo_plan(store, batch_id)
        report = execute_undo(plan, store=store, **kwargs)
        return plan, report

    # ------------------------------------------------------------- 断言助手
    def by_src(self, items):
        """把 items/results 按原始路径索引，避免依赖内部顺序。"""
        return {item.src: item for item in items}

    def dst_of(self, report, name):
        """从整理报告里取出某个文件被搬去的目标路径。"""
        matches = [r.dst for r in report.results if Path(r.src).name == name]
        self.assertEqual(len(matches), 1, f"报告里找不到唯一的 {name}")
        return matches[0]

    def read_raw(self) -> list:
        """逐行读记录文件（撤销的"台账"），坏行会让测试直接失败。"""
        if not self.records_path.exists():
            return []
        with open(self.records_path, "r", encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]


# ====================================================================== 场景 1
class TestNormalUndo(UndoTempCase):
    """正常撤销：文件回到原位，分类文件夹不残留，内容一个字节都不许变。"""

    def test_files_restored_with_identical_content(self):
        before = self.expected_md5()
        report = self.organize()

        # 整理后：原位置第一层空了，分类文件夹里各就各位
        self.assertEqual(top_files(self.root), [])
        self.assertEqual(len(all_files(self.root)), 4)

        plan, undo_report = self.undo(report.batch_id)

        self.assertEqual(plan.total, 4)
        self.assertEqual(plan.ready_count, 4, "4 个 moved 记录都应该可以撤销")
        self.assertEqual(undo_report.total, 4)
        self.assertEqual(undo_report.restored, 4)
        self.assertEqual(undo_report.skipped, 0)
        self.assertEqual(undo_report.failed, 0)
        self.assertFalse(undo_report.stopped)
        self.assertEqual(undo_report.remaining, 0)
        self.assertEqual(undo_report.record_errors, 0)

        # ---- 磁盘真实状态：原位有文件、内容 md5 与整理前完全一致 ----
        for name, fingerprint in before.items():
            src = self.root / name
            self.assertTrue(src.is_file(), f"{name} 没有回到原位")
            self.assertEqual(md5(src), fingerprint, f"{name} 的内容被改动了")
            self.assertEqual(src.read_bytes(), self.FILES[name])

        self.assertEqual(top_files(self.root), sorted(before))
        self.assertEqual(len(all_files(self.root)), 4, "恢复后不该多出副本")

        # ---- 分类文件夹里没有残留文件（文件夹本身可以留下）----
        for category in ("图片", "文档", "视频", "压缩包"):
            self.assertEqual(
                all_files(self.root / category), [],
                f"分类文件夹 {category} 里还有残留文件",
            )
        for result in report.results:
            self.assertFalse(os.path.exists(result.dst), f"{result.dst} 应该已经被搬走")

    def test_undo_records_are_written_in_expected_shape(self):
        report = self.organize()
        self.undo(report.batch_id)

        undo_records = [e for e in self.read_raw() if e.get("undo_of")]
        self.assertEqual(len(undo_records), 4, "每个处理过的文件都该留一条撤销记录")

        undo_ids = set()
        for entry in undo_records:
            self.assertEqual(entry["undo_of"], report.batch_id)
            self.assertEqual(entry["phase"], PHASE_UNDO)
            self.assertTrue(entry["batch_id"].startswith("undo-"))
            self.assertEqual(entry["status"], STATUS_RESTORED)
            self.assertTrue(entry["src"] and entry["dst"] and entry["ts"])
            undo_ids.add(entry["batch_id"])
        self.assertEqual(len(undo_ids), 1, "一次撤销只应该有一个 undo 批次号")

    def test_nothing_outside_batch_is_touched(self):
        """撤销一个批次，不能碰另一个批次的文件。"""
        first = self.organize(["照片.jpg", "论文.pdf"])

        # 第二个批次：用户又往目录里放了新文件，再整理一次
        (self.root / "笔记.txt").write_bytes(b"note-content")
        (self.root / "截图.png").write_bytes(b"png-content")
        second = self.run_organize(expect=2)
        self.assertNotEqual(first.batch_id, second.batch_id)

        _, undo_report = self.undo(first.batch_id)
        self.assertEqual(undo_report.restored, 2)

        # 第二批的文件必须还在分类文件夹里，一个都没被搬回来
        for result in second.results:
            self.assertTrue(os.path.isfile(result.dst), f"{result.dst} 被误撤销了")
            self.assertFalse(os.path.exists(result.src), f"{result.src} 不该出现")

        # 第一批的文件确实回来了
        self.assertTrue((self.root / "照片.jpg").is_file())
        self.assertTrue((self.root / "论文.pdf").is_file())

    def test_stale_batch_cannot_steal_file_moved_again(self):
        """撤销过又整理了一次：拿旧批次再撤销，不许把新批次的文件搬走。

        记录里 A 批次是 restored 状态，但文件又被 B 批次搬进了分类文件夹。
        这时候再点 A 的撤销，必须只看到"已经撤销过"，绝不能动 B 的文件。
        """
        report_a = self.organize(["照片.jpg"])
        dst_a = self.dst_of(report_a, "照片.jpg")

        _, undo_a = self.undo(report_a.batch_id)
        self.assertEqual(undo_a.restored, 1)
        self.assertTrue((self.root / "照片.jpg").is_file())

        report_b = self.run_organize(expect=1)
        self.assertNotEqual(report_a.batch_id, report_b.batch_id)
        self.assertTrue((self.root / "照片.jpg").exists() is False)
        moved_by_b = report_b.results[0].dst
        self.assertTrue(os.path.isfile(moved_by_b))

        # 拿旧批次 A 再撤销一次：一条都不能搬
        plan_a, again = self.undo(report_a.batch_id)
        self.assertEqual(again.restored, 0)
        self.assertEqual(plan_a.ready_count, 0)
        self.assertTrue(
            any(REASON_ALREADY_UNDONE in i.message for i in plan_a.items),
            "旧批次的提示应该是：已经撤销过",
        )
        self.assertTrue(os.path.isfile(moved_by_b), "B 批次的文件被 A 的旧记录搬走了！")
        self.assertFalse((self.root / "照片.jpg").exists())

        # 该批次 B 自己仍然能正常撤销
        _, undo_b = self.undo(report_b.batch_id)
        self.assertEqual(undo_b.restored, 1)
        self.assertTrue((self.root / "照片.jpg").is_file())
        self.assertFalse(os.path.exists(moved_by_b))
        self.assertEqual(os.path.dirname(dst_a), os.path.dirname(moved_by_b))


# ====================================================================== 场景 2
class TestSourceConflict(UndoTempCase):
    """原始路径上已经有一个同名文件：跳过，绝不覆盖，两边都留在原处。"""

    def test_same_name_at_source_is_skipped(self):
        report = self.organize(["照片.jpg", "论文.pdf"])
        photo_dst = self.dst_of(report, "照片.jpg")
        paper_dst = self.dst_of(report, "论文.pdf")

        # 撤销前，在原位置放一个同名文件（内容不同，方便看清有没有被覆盖）
        blocker = self.root / "照片.jpg"
        blocker.write_bytes(b"blocker-should-never-be-overwritten")
        blocker_md5 = md5(blocker)
        moved_md5 = md5(photo_dst)

        plan, undo_report = self.undo(report.batch_id)

        item = self.by_src(plan.items)[str(blocker)]
        self.assertEqual(item.status, STATUS_SKIPPED)
        self.assertIn("同名文件", item.message)

        self.assertEqual(plan.ready_count, 1)
        self.assertEqual(plan.skipped_count, 1)
        self.assertEqual(undo_report.restored, 1, "只有没被挡住的那个文件能恢复")
        self.assertEqual(undo_report.failed, 0)

        # ---- 拦路的同名文件一个字节都没变 ----
        self.assertEqual(blocker.read_bytes(), b"blocker-should-never-be-overwritten")
        self.assertEqual(md5(blocker), blocker_md5)

        # ---- 被挡住的文件仍然老老实实留在分类文件夹里 ----
        self.assertTrue(os.path.isfile(photo_dst), "被挡住的文件不能凭空消失")
        self.assertEqual(md5(photo_dst), moved_md5)

        # ---- 另一个文件正常回位 ----
        self.assertTrue((self.root / "论文.pdf").is_file())
        self.assertEqual(md5(self.root / "论文.pdf"), self.expected_md5(["论文.pdf"])["论文.pdf"])
        self.assertFalse(os.path.exists(paper_dst))

        # 记录里只写"真动过"的文件：方案阶段就跳过的条目没有写记录（它压根没被碰过），
        # 所以这个被挡住的文件在下次打开历史时仍然算"可撤销"，用户可以清掉同名文件再试一次。
        undo_records = [e for e in self.read_raw() if e.get("undo_of")]
        statuses = {e["src"]: e["status"] for e in undo_records}
        self.assertEqual(len(undo_records), 1)
        self.assertNotIn(str(blocker), statuses, "没搬的文件不该写撤销记录")
        self.assertEqual(
            list_batches(RecordStore(self.records_path))[0].undoable, 1,
            "被挡住的文件应该还留在可撤销列表里",
        )

    def test_retry_after_user_removes_blocker(self):
        """真实流程：被同名文件挡住 → 用户删掉它 → 再点一次撤销，应该能成功。"""
        report = self.organize(["照片.jpg", "论文.pdf"])
        photo_dst = self.dst_of(report, "照片.jpg")
        blocker = self.root / "照片.jpg"
        blocker.write_bytes(b"blocker")

        _, first = self.undo(report.batch_id)
        self.assertEqual(first.restored, 1)
        self.assertEqual(list_batches(RecordStore(self.records_path))[0].undoable, 1)

        delete_path(blocker)
        _, second = self.undo(report.batch_id)

        self.assertEqual(second.restored, 1)
        self.assertEqual(md5(self.root / "照片.jpg"), self.expected_md5(["照片.jpg"])["照片.jpg"])
        self.assertFalse(os.path.exists(photo_dst))
        self.assertEqual(list_batches(RecordStore(self.records_path))[0].undoable, 0)


# ====================================================================== 场景 3
class TestDstMissing(UndoTempCase):
    """目标文件已经不存在（被用户手动删了/移了）：跳过，原因要说清。"""

    def test_missing_dst_is_skipped_with_reason(self):
        report = self.organize(["照片.jpg", "论文.pdf"])
        photo_dst = self.dst_of(report, "照片.jpg")
        paper_dst = self.dst_of(report, "论文.pdf")
        delete_path(photo_dst)

        plan, undo_report = self.undo(report.batch_id)

        item = self.by_src(plan.items)[str(self.root / "照片.jpg")]
        self.assertEqual(item.status, STATUS_SKIPPED)
        self.assertIn("不存在", item.message)

        self.assertEqual(plan.skipped_count, 1)
        self.assertEqual(undo_report.restored, 1)

        # 被删掉的那个不会被"凭空造回来"
        self.assertFalse(os.path.exists(self.root / "照片.jpg"))
        self.assertTrue((self.root / "论文.pdf").is_file())
        self.assertFalse(os.path.exists(paper_dst))


# ====================================================================== 场景 4
class TestContentChanged(UndoTempCase):
    """移动后文件内容被改过：仍然要搬回去，绝不能因为"内容变了"就丢文件。"""

    def test_changed_content_is_still_restored(self):
        report = self.organize(["论文.pdf"])
        dst = self.dst_of(report, "论文.pdf")

        changed = b"pdf-content-EDITED-BY-USER-longer-than-before"
        Path(dst).write_bytes(changed)

        plan, undo_report = self.undo(report.batch_id)

        self.assertEqual(plan.ready_count, 1, "内容变化不影响可撤销性")
        self.assertEqual(undo_report.restored, 1)
        self.assertEqual(undo_report.skipped, 0)

        src = self.root / "论文.pdf"
        self.assertTrue(src.is_file(), "内容变了也必须搬回来")
        self.assertEqual(src.read_bytes(), changed, "恢复的必须是用户改过之后的那份内容")
        self.assertEqual(md5(src), hashlib.md5(changed).hexdigest())
        self.assertFalse(os.path.exists(dst))

        # 实现语义：大小校验比的是"撤销前那一刻 dst 的大小"，
        # 所以用户主动改内容不会被判成异常（unverified=0），也不会被判失败。
        self.assertEqual(undo_report.unverified, 0)
        self.assertEqual(undo_report.failed, 0)

    def test_truncated_content_is_not_lost(self):
        """内容被截短（大小变化）也不能丢文件——这是最容易被"大小校验"坑到的情形。"""
        report = self.organize(["电影.mp4"])
        dst = self.dst_of(report, "电影.mp4")
        Path(dst).write_bytes(b"")

        _, undo_report = self.undo(report.batch_id)

        self.assertEqual(undo_report.restored, 1)
        src = self.root / "电影.mp4"
        self.assertTrue(src.is_file())
        self.assertEqual(src.read_bytes(), b"")
        self.assertFalse(os.path.exists(dst))
        # 文件已经回到原位，即使"看起来不对"也只能记 restored，不能记 failed
        self.assertEqual(undo_report.failed, 0)


# ====================================================================== 场景 5
class TestOutsideRoot(UndoTempCase):
    """记录里的路径越界（被人改过）：跳过，目录外的文件一根手指都不许碰。"""

    def test_dst_outside_root_is_skipped(self):
        report = self.organize(["照片.jpg", "论文.pdf"])

        # 目录外造一个"真有内容"的文件，用来证明它没被动过
        outside = self.case_dir / "外部目录"
        (outside / "文档").mkdir(parents=True)
        secret = outside / "文档" / "机密.txt"
        secret.write_bytes(b"secret-outside-content")
        secret_md5 = md5(secret)

        # 手写一条"看似合法"的 moved 记录，但 dst 指向整理目录外面
        self.store.append({
            "ts": now_text(),
            "batch_id": report.batch_id,
            "phase": "end",
            "status": "moved",
            "src": str(self.root / "机密.txt"),
            "dst": str(secret),
            "category": "文档",
            "message": "手工伪造的越界记录",
        })

        plan, undo_report = self.undo(report.batch_id)

        forged = self.by_src(plan.items)[str(self.root / "机密.txt")]
        self.assertEqual(forged.status, STATUS_SKIPPED)
        self.assertTrue(
            ("不一致" in forged.message)
            or ("不在整理目录内" in forged.message)
            or ("越界" in forged.message),
            f"跳过原因没说明白：{forged.message}",
        )

        # ---- 目录外的文件原地不动 ----
        self.assertTrue(secret.is_file(), "目录外的文件被搬走了！")
        self.assertEqual(md5(secret), secret_md5, "目录外的文件内容被改了！")
        self.assertFalse(os.path.exists(self.root / "机密.txt"), "不能伪造出一个文件")

        # 正常的两条照旧恢复
        self.assertEqual(plan.skipped_count, 1)
        self.assertEqual(undo_report.restored, 2)

    def test_src_outside_root_is_skipped(self):
        """反过来把 src 指向目录外：同样必须拦下。"""
        report = self.organize(["照片.jpg"])

        outside_dir = self.case_dir / "别处"
        outside_dir.mkdir()
        victim = outside_dir / "照片.jpg"
        victim.write_bytes(b"victim-outside-content")
        victim_md5 = md5(victim)

        self.store.append({
            "ts": now_text(),
            "batch_id": report.batch_id,
            "phase": "end",
            "status": "moved",
            "src": str(victim),                     # 越界的 src
            "dst": str(self.root / "图片" / "照片.jpg"),
            "category": "图片",
            "message": "手工伪造的越界记录",
        })

        plan, _ = self.undo(report.batch_id)

        forged = self.by_src(plan.items)[str(victim)]
        self.assertEqual(forged.status, STATUS_SKIPPED)
        self.assertIn("不在整理目录内", forged.message)
        self.assertTrue(victim.is_file())
        self.assertEqual(md5(victim), victim_md5)

    def test_dst_is_a_symlink_is_skipped(self):
        """分类文件夹里的"文件"被换成了链接：不能顺着链接把目录外的文件搬走。"""
        report = self.organize(["照片.jpg"])
        dst = Path(self.dst_of(report, "照片.jpg"))

        outside = self.case_dir / "外部目标.txt"
        outside.write_bytes(b"outside-target-content")
        outside_md5 = md5(outside)

        # 把分类文件夹里的文件换成指向目录外的符号链接
        delete_path(dst)
        try:
            os.symlink(str(outside), str(dst))
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前系统不允许创建符号链接：{exc}")
        if not os.path.islink(str(dst)):
            # 有些安全策略会把 symlink 调用"降级"成普通空文件，这时用例没有意义
            self.skipTest("当前环境没有真正创建出符号链接（系统策略拦截）")

        plan, undo_report = self.undo(report.batch_id)

        item = self.by_src(plan.items)[str(self.root / "照片.jpg")]
        self.assertEqual(item.status, STATUS_SKIPPED)
        self.assertIn("链接", item.message)

        # 目录外的目标文件一个字节都没变，链接本身也还在原处
        self.assertTrue(outside.is_file())
        self.assertEqual(md5(outside), outside_md5)
        self.assertEqual(undo_report.restored, 0)
        self.assertFalse(os.path.exists(self.root / "照片.jpg"))

    def test_category_folder_replaced_by_junction_is_skipped(self):
        """分类文件夹被换成指向目录外的目录联接：realpath 必须识破，绕不出去。"""
        report = self.organize(["照片.jpg"])
        real_folder = self.root / "图片"
        backup = self.root / "图片_备份"
        real_folder.rename(backup)

        outside = self.case_dir / "外部目录"
        outside.mkdir()
        (outside / "别人的文件.txt").write_bytes(b"other-people-content")
        outside_md5 = md5(outside / "别人的文件.txt")

        result = os.system(
            f'cmd /c mklink /J "{real_folder}" "{outside}" >nul 2>&1'
        )
        if result != 0 or not os.path.isdir(str(real_folder)):
            self.skipTest("当前系统无法创建目录联接")

        try:
            plan, undo_report = self.undo(report.batch_id)

            item = self.by_src(plan.items)[str(self.root / "照片.jpg")]
            self.assertEqual(item.status, STATUS_SKIPPED, "目录联接指向外面时必须跳过")
            self.assertTrue(
                ("分类文件夹" in item.message) or ("不在整理目录内" in item.message),
                f"跳过原因没说清：{item.message}",
            )
            self.assertEqual(undo_report.restored, 0)

            # 目录外的东西不能被搬动，备份里的原文件也还在
            self.assertEqual(md5(outside / "别人的文件.txt"), outside_md5)
            self.assertFalse(os.path.exists(self.root / "照片.jpg"))
            self.assertEqual(
                md5(backup / "照片.jpg"),
                self.expected_md5(["照片.jpg"])["照片.jpg"],
            )
        finally:
            try:
                delete_path(real_folder)        # 只删联接本身，不影响外面的目录
            except OSError:
                pass


# ====================================================================== 场景 6
class TestPartialSuccess(UndoTempCase):
    """部分成功部分跳过：统计数字必须和磁盘现状一致，不能"看着像成功"。"""

    def test_one_conflict_one_normal(self):
        report = self.organize(["照片.jpg", "论文.pdf"])
        photo_dst = self.dst_of(report, "照片.jpg")
        paper_dst = self.dst_of(report, "论文.pdf")

        (self.root / "照片.jpg").write_bytes(b"blocker")

        plan, undo_report = self.undo(report.batch_id)

        self.assertEqual(plan.ready_count, 1)
        self.assertEqual(plan.skipped_count, 1)
        self.assertEqual(undo_report.restored, 1)
        self.assertEqual(undo_report.failed, 0)

        # 界面口径：方案阶段跳过 + 执行阶段跳过 = 本次一共跳过几个
        pre_skipped = len([i for i in plan.items if i.status == STATUS_SKIPPED])
        total_skipped = undo_report.skipped + pre_skipped
        self.assertEqual(total_skipped, 1)
        self.assertEqual(
            undo_report.restored + total_skipped + undo_report.failed,
            2, "统计口径要能对上一共两层文件",
        )

        # 数字和磁盘对得上
        self.assertTrue((self.root / "论文.pdf").is_file())
        self.assertFalse(os.path.exists(paper_dst))
        self.assertEqual((self.root / "照片.jpg").read_bytes(), b"blocker")
        self.assertTrue(os.path.isfile(photo_dst))

        self.assertIn("成功恢复 1 个", undo_report.summary_text())


# ====================================================================== 场景 7
class TestUserCancel(UndoTempCase):
    """用户中途点停止：停就得真停，剩下的文件必须原样留在分类文件夹里。"""

    def test_stop_after_first_file(self):
        report = self.organize()          # 4 个文件
        progress = []
        calls = {"n": 0}

        def should_cancel():
            calls["n"] += 1
            return calls["n"] > 1         # 第一次问"不停"，第二次问"停"

        plan, undo_report = self.undo(
            report.batch_id,
            on_progress=lambda done, total: progress.append((done, total)),
            should_cancel=should_cancel,
        )

        self.assertTrue(undo_report.stopped)
        self.assertEqual(len(undo_report.results), 1, "停止后不能再处理文件")
        self.assertEqual(undo_report.total, 4)
        self.assertEqual(undo_report.remaining, 3)
        self.assertEqual(undo_report.restored, 1)
        self.assertEqual(progress, [(1, 4)])

        restored = undo_report.results[0]
        self.assertTrue(os.path.isfile(restored.src))
        self.assertFalse(os.path.exists(restored.dst))

        # 没处理的那 3 个必须原地不动
        untouched = [i for i in plan.items if i.src != restored.src]
        self.assertEqual(len(untouched), 3)
        for item in untouched:
            self.assertTrue(os.path.isfile(item.dst), f"{item.dst} 不该被处理")
            self.assertFalse(os.path.exists(item.src), f"{item.src} 不该出现")

        # 记录里只该有 1 条撤销记录
        undo_records = [e for e in self.read_raw() if e.get("undo_of")]
        self.assertEqual(len(undo_records), 1)
        self.assertIn("因停止未处理 3 个", undo_report.summary_text())

    def test_cancel_before_first_file_touches_nothing(self):
        """一进来就点了停止：一个文件都不能碰。"""
        report = self.organize(["照片.jpg", "论文.pdf"])
        plan, undo_report = self.undo(report.batch_id, should_cancel=lambda: True)

        self.assertTrue(undo_report.stopped)
        self.assertEqual(undo_report.results, [])
        self.assertEqual(undo_report.restored, 0)
        self.assertEqual(undo_report.remaining, 2)
        for item in plan.items:
            self.assertTrue(os.path.isfile(item.dst), "文件不该被搬走")
            self.assertFalse(os.path.exists(item.src))
        self.assertEqual([e for e in self.read_raw() if e.get("undo_of")], [])


# ====================================================================== 场景 8
class TestAfterRestart(UndoTempCase):
    """关掉程序再打开：从磁盘记录能重新列出批次并撤销成功。"""

    def test_new_store_sees_batch_and_undoes_it(self):
        before = self.expected_md5(["照片.jpg", "论文.pdf", "电影.mp4"])
        first_report = self.organize(["照片.jpg", "论文.pdf", "电影.mp4"])

        # ---- 模拟重启：全新实例、全新对象，什么都不共享 ----
        restarted = RecordStore(self.records_path)
        self.assertIsNot(restarted, self.store)

        batches = list_batches(restarted)
        self.assertEqual(len(batches), 1)
        summary = batches[0]
        self.assertEqual(summary.batch_id, first_report.batch_id)
        self.assertEqual(summary.moved, 3)
        self.assertEqual(summary.undoable, 3)
        self.assertEqual(summary.uncertain, 0)
        self.assertEqual(
            os.path.normcase(os.path.abspath(summary.root)),
            os.path.normcase(os.path.abspath(str(self.root))),
            f"从记录反推出的整理目录不对：{summary.root}",
        )

        plan = build_undo_plan(restarted, summary.batch_id)
        undo_report = execute_undo(plan, store=restarted)

        self.assertEqual(undo_report.restored, 3)
        for name, fingerprint in before.items():
            self.assertTrue((self.root / name).is_file())
            self.assertEqual(md5(self.root / name), fingerprint)

        # 撤销后再用**又一个**新实例读：批次还在，但已无可撤销文件
        third = RecordStore(self.records_path)
        batches_after = list_batches(third)
        self.assertEqual(len(batches_after), 1, "撤销批次不能被当成整理批次列出来")
        self.assertEqual(batches_after[0].undoable, 0)
        self.assertEqual(
            batches_after[0].label,
            f"{batches_after[0].time_text} · 无可撤销文件",
        )


# ====================================================================== 场景 9
class TestDuplicateUndo(UndoTempCase):
    """连着点两次「撤销」：第二次一条都不能搬，记录也不能越写越乱。"""

    def test_second_undo_does_nothing(self):
        names = ["照片.jpg", "论文.pdf", "工具.zip"]
        before = self.expected_md5(names)
        report = self.organize(names)

        _, first = self.undo(report.batch_id)
        self.assertEqual(first.restored, 3)

        raw_after_first = len(self.read_raw())

        plan2, second = self.undo(report.batch_id)

        # ---- 第二次：方案层面全部 skipped，执行层面没有任何文件被动 ----
        self.assertEqual(second.restored, 0, "第二次绝不能重复恢复")
        self.assertEqual(second.results, [], "没有 ready 条目就不该进入执行阶段")
        self.assertEqual(plan2.ready_count, 0)
        self.assertEqual(plan2.skipped_count, 3)
        for item in plan2.items:
            self.assertTrue(
                ("撤销过" in item.message) or ("不存在" in item.message),
                f"第二次点击的提示不对劲：{item.message}",
            )
        self.assertTrue(
            any(REASON_ALREADY_UNDONE in i.message for i in plan2.items),
            "至少应该有一条明确提示已经撤销过",
        )

        # ---- 文件只被恢复了一次：原位各一份、内容不变、分类目录空 ----
        for name, fingerprint in before.items():
            self.assertEqual(md5(self.root / name), fingerprint)
        self.assertEqual(top_files(self.root), sorted(before))   # 没有多出副本
        for category in ("图片", "文档", "压缩包"):
            self.assertEqual(all_files(self.root / category), [])

        # ---- 记录文件没被写坏：行数不增、每行都是合法 JSON ----
        raw = self.read_raw()                   # 坏行会让 json.loads 抛异常
        self.assertEqual(len(raw), raw_after_first, "第二次没搬文件就不该再写记录")
        self.assertEqual(len(raw), 3 * 2 + 3)   # 3 个文件各 begin+end，加 3 条撤销记录
        self.assertEqual(len(RecordStore(self.records_path).read_all()), 6)


# ====================================================================== 场景 10
class TestRecordWriteFailure(UndoTempCase):
    """撤销记录写不进文件：文件照样要恢复，而且必须如实报错（不许谎报成功）。"""

    class BrokenStore(RecordStore):
        """append 永远失败，用来模拟磁盘满/文件被占用。"""

        def __init__(self, path):
            super().__init__(path)
            self.append_calls = 0

        def append(self, entry):
            self.append_calls += 1
            raise RecordError("模拟：操作记录写入失败")

    def test_files_restored_even_if_records_fail(self):
        names = ["照片.jpg", "论文.pdf", "电影.mp4"]
        before = self.expected_md5(names)
        report = self.organize(names)
        raw_before = len(self.read_raw())

        broken = self.BrokenStore(self.records_path)
        plan = build_undo_plan(broken, report.batch_id)
        undo_report = execute_undo(plan, store=broken)

        # ---- 文件该恢复还是要恢复（记录失败不能拖累文件安全）----
        self.assertEqual(undo_report.restored, 3)
        for name, fingerprint in before.items():
            self.assertTrue((self.root / name).is_file())
            self.assertEqual(md5(self.root / name), fingerprint)
        for result in report.results:
            self.assertFalse(os.path.exists(result.dst))

        # ---- 但必须如实上报：record_errors > 0 且点名了哪些文件 ----
        self.assertEqual(undo_report.record_errors, 3)
        self.assertGreater(undo_report.record_errors, 0, "绝不能谎报成功")
        self.assertEqual(len(undo_report.record_failed_items), 3)
        self.assertEqual(
            undo_report.record_failed_items,
            sorted(str(self.root / name) for name in before),
        )
        # 每条记录都试了两次（当场一次 + 收尾重试一次）
        self.assertEqual(broken.append_calls, 6)

        # ---- 记录文件本身没有被写脏：仍然只有整理阶段的记录 ----
        raw = self.read_raw()
        self.assertEqual(len(raw), raw_before)
        self.assertFalse([e for e in raw if e.get("undo_of")])

    def test_transient_failure_is_retried_and_succeeds(self):
        """偶发失败（第一次写不进、重试成功）：不算失败，且记录能补上。"""

        class FlakyStore(RecordStore):
            def __init__(self, path):
                super().__init__(path)
                self.failed_srcs = set()

            def append(self, entry):
                key = entry.get("src")
                if key not in self.failed_srcs:
                    self.failed_srcs.add(key)
                    raise RecordError("模拟：第一次写失败")
                return super().append(entry)

        names = ["照片.jpg", "论文.pdf"]
        report = self.organize(names)

        flaky = FlakyStore(self.records_path)
        plan = build_undo_plan(flaky, report.batch_id)
        undo_report = execute_undo(plan, store=flaky)

        self.assertEqual(undo_report.restored, 2)
        self.assertEqual(undo_report.record_errors, 0)
        self.assertEqual(undo_report.record_failed_items, [])
        self.assertEqual(len([e for e in self.read_raw() if e.get("undo_of")]), 2)
        for name in names:
            self.assertTrue((self.root / name).is_file())


# ====================================================================== 补充：批次列表
class TestListBatches(UndoTempCase):
    """历史批次列表：只列真正的整理批次，撤销批次不许冒充。"""

    def test_undo_batch_is_not_listed_as_original(self):
        report = self.organize(["照片.jpg", "论文.pdf"])
        self.undo(report.batch_id)

        undo_records = [e for e in self.read_raw() if e.get("undo_of")]
        self.assertTrue(undo_records)
        undo_ids = {e["batch_id"] for e in undo_records}
        self.assertFalse([b for b in undo_ids if not b.startswith("undo-")])

        batches = list_batches(RecordStore(self.records_path))
        listed = {b.batch_id for b in batches}
        self.assertEqual(listed, {report.batch_id})
        self.assertFalse(undo_ids & listed, "撤销批次出现在历史列表里了")

    def test_counts_and_label(self):
        report = self.organize(["照片.jpg", "论文.pdf", "电影.mp4"])
        # 撤销前在原位置占一个名字，不影响列表统计
        (self.root / "照片.jpg").write_bytes(b"blocker")

        summary = list_batches(self.store)[0]
        self.assertEqual(summary.batch_id, report.batch_id)
        self.assertEqual(summary.moved, 3)
        self.assertEqual(summary.skipped, 0)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.uncertain, 0)
        self.assertEqual(summary.undoable, 3)
        self.assertIn("可撤销 3 个", summary.label)
        self.assertIn(summary.time_text, summary.label)

    def test_skipped_and_uncertain_are_counted(self):
        """整理时被跳过/失败、以及中途断掉的（moving）都要单独计数。"""
        batch = "手工批次-001"
        moved_file = self.root / "图片" / "照片.jpg"
        moved_file.parent.mkdir(parents=True)
        moved_file.write_bytes(b"photo")
        # 这条 begin 会被同 (batch_id, src) 的 end 合并掉，这是记录层的既定规则
        self.store.append({
            "ts": "2026-10-18 10:00:00", "batch_id": batch, "phase": "begin",
            "status": "moving", "src": str(self.root / "照片.jpg"),
            "dst": str(moved_file), "category": "图片", "message": "准备移动",
        })
        self.store.append({
            "ts": "2026-10-18 10:00:01", "batch_id": batch, "phase": "end",
            "status": "moved", "src": str(self.root / "照片.jpg"),
            "dst": str(moved_file), "category": "图片", "message": "已移动",
        })
        self.store.append({
            "ts": "2026-10-18 10:00:02", "batch_id": batch, "phase": "end",
            "status": "skipped", "src": str(self.root / "论文.pdf"),
            "dst": str(self.root / "文档" / "论文.pdf"), "category": "文档",
            "message": "目标位置已有同名文件",
        })
        self.store.append({
            "ts": "2026-10-18 10:00:03", "batch_id": batch, "phase": "end",
            "status": "failed", "src": str(self.root / "电影.mp4"),
            "dst": str(self.root / "视频" / "电影.mp4"), "category": "视频",
            "message": "被占用",
        })
        self.store.append({
            "ts": "2026-10-18 10:00:04", "batch_id": batch, "phase": "begin",
            "status": "moving", "src": str(self.root / "工具.zip"),
            "dst": str(self.root / "压缩包" / "工具.zip"), "category": "压缩包",
            "message": "准备移动",
        })

        summary = list_batches(self.store)[0]
        self.assertEqual(summary.moved, 1)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.uncertain, 1)
        self.assertEqual(summary.untouched, 2)
        self.assertEqual(summary.undoable, 1)
        # 合并后剩下最早的 ts 是 10:00:01（10:00:00 那条 begin 已被 end 覆盖）
        self.assertEqual(summary.started_at, "2026-10-18 10:00:01")

    def test_batches_sorted_newest_first(self):
        for index, stamp in enumerate(
            ("2026-10-01 09:00:00", "2026-10-18 21:30:00", "2026-10-10 12:00:00")
        ):
            self.store.append({
                "ts": stamp, "batch_id": f"batch-{index}", "phase": "end",
                "status": "skipped", "src": str(self.root / f"f{index}.txt"),
                "dst": str(self.root / "文档" / f"f{index}.txt"),
                "category": "文档", "message": "跳过",
            })

        stamps = [b.started_at for b in list_batches(self.store)]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(stamps[0], "2026-10-18 21:30:00")

    def test_empty_store_returns_empty_list(self):
        self.assertEqual(list_batches(self.store), [])


# ====================================================================== 补充：moving
class TestMovingStatus(UndoTempCase):
    """status="moving" 表示上次整理中途断了：一律跳过，绝不当成"搬走了"。"""

    def test_moving_record_is_never_restored(self):
        report = self.organize(["照片.jpg"])
        photo_dst = self.dst_of(report, "照片.jpg")

        interrupted_src = self.root / "半途.pdf"
        interrupted_dst = self.root / "文档" / "半途.pdf"
        interrupted_dst.parent.mkdir(parents=True, exist_ok=True)
        interrupted_dst.write_bytes(b"half-way-content")

        self.store.append({
            "ts": now_text(), "batch_id": report.batch_id, "phase": "begin",
            "status": "moving", "src": str(interrupted_src),
            "dst": str(interrupted_dst), "category": "文档",
            "message": "准备移动",
        })

        plan, undo_report = self.undo(report.batch_id)

        item = self.by_src(plan.items)[str(interrupted_src)]
        self.assertEqual(item.status, STATUS_SKIPPED, "moving 记录不许进 ready")
        self.assertIn("状态不确定", item.message)

        # 状态不确定的文件原地不动
        self.assertTrue(interrupted_dst.is_file())
        self.assertEqual(interrupted_dst.read_bytes(), b"half-way-content")
        self.assertFalse(interrupted_src.exists())

        self.assertEqual(plan.ready_count, 1)
        self.assertEqual(plan.skipped_count, 1)
        self.assertEqual(undo_report.restored, 1, "只有状态明确的那个能恢复")
        self.assertTrue(os.path.isfile(self.root / "照片.jpg"))
        self.assertFalse(os.path.exists(photo_dst))

    def test_even_if_dst_is_gone_moving_is_still_skipped(self):
        """dst 也不存在时，仍然按"状态不确定"处理，而不是"已不存在"。"""
        batch = "中断批次"
        self.store.append({
            "ts": now_text(), "batch_id": batch, "phase": "begin",
            "status": "moving", "src": str(self.root / "半途.pdf"),
            "dst": str(self.root / "文档" / "半途.pdf"), "category": "文档",
            "message": "准备移动",
        })

        plan = build_undo_plan(self.store, batch)
        self.assertEqual(plan.total, 1)
        self.assertEqual(plan.ready_count, 0)
        self.assertEqual(plan.items[0].status, STATUS_SKIPPED)
        self.assertIn("状态不确定", plan.items[0].message)

    def test_moving_record_blocks_execution_even_with_store(self):
        """moving 记录的批次：方案里 ready 为空，就算硬执行也一条都搬不动。"""
        batch = "中断批次-2"
        dst = self.root / "文档" / "半途.pdf"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"content")
        self.store.append({
            "ts": "2026-10-18 09:00:00", "batch_id": batch, "phase": "begin",
            "status": "moving", "src": str(self.root / "半途.pdf"),
            "dst": str(dst), "category": "文档", "message": "准备移动",
        })

        plan = build_undo_plan(self.store, batch)
        self.assertEqual(plan.ready_count, 0)
        # 即使硬着头皮执行，也一条都不能搬（ready 为空）
        report = execute_undo(plan, store=self.store)
        self.assertEqual(report.restored, 0)
        self.assertTrue(dst.is_file())
        self.assertFalse((self.root / "半途.pdf").exists())


# ====================================================================== 补充：执行前二次检查
class TestPlanExecuteRace(UndoTempCase):
    """方案生成之后、点确认之前，磁盘又变了：真正动手前必须再查一遍。

    这是 undoer 模块注释里明确承诺的"两次检查内容完全一样"，
    也是"用户看完列表发呆两分钟再点撤销"这种真实场景。
    """

    def test_changes_between_plan_and_execute_are_caught(self):
        report = self.organize(["照片.jpg", "论文.pdf"])
        photo_dst = self.dst_of(report, "照片.jpg")
        paper_dst = self.dst_of(report, "论文.pdf")

        plan = build_undo_plan(self.store, report.batch_id)
        self.assertEqual(plan.ready_count, 2, "方案阶段两条都应该是 ready")

        # ---- 生成方案之后，磁盘变了：一个位置出现同名文件，一个目标被删 ----
        blocker = self.root / "照片.jpg"
        blocker.write_bytes(b"late-blocker")
        delete_path(paper_dst)

        undo_report = execute_undo(plan, store=self.store)

        # 执行阶段必须重新判定，而不是照着旧方案硬搬
        self.assertEqual(undo_report.restored, 0)
        self.assertEqual(undo_report.skipped, 2)
        self.assertEqual(undo_report.failed, 0)

        reasons = {Path(r.src).name: r.message for r in undo_report.results}
        self.assertIn("同名文件", reasons["照片.jpg"])
        self.assertIn("不存在", reasons["论文.pdf"])

        # 磁盘状态：同名文件没被覆盖，被删的文件没被凭空造出来
        self.assertEqual(blocker.read_bytes(), b"late-blocker")
        self.assertTrue(os.path.isfile(photo_dst))
        self.assertFalse(os.path.exists(self.root / "论文.pdf"))

    def test_dst_removed_after_plan_is_skipped_not_failed(self):
        report = self.organize(["电影.mp4"])
        dst = self.dst_of(report, "电影.mp4")
        plan = build_undo_plan(self.store, report.batch_id)
        delete_path(dst)

        undo_report = execute_undo(plan, store=self.store)

        self.assertEqual(undo_report.restored, 0)
        self.assertEqual(undo_report.skipped, 1, "别人删掉的文件不该记成撤销失败")
        self.assertEqual(undo_report.failed, 0)
        self.assertFalse(os.path.exists(self.root / "电影.mp4"))


# ====================================================================== 补充：失败分支
class TestUndoFailure(UndoTempCase):
    """文件被别的程序占用：记 failed（不是 skipped），文件留在分类文件夹、可以重试。"""

    def test_locked_file_reports_failed_and_stays_in_category(self):
        report = self.organize(["照片.jpg", "论文.pdf"])
        locked_dst = self.dst_of(report, "照片.jpg")
        paper_dst = self.dst_of(report, "论文.pdf")

        handle = open(locked_dst, "rb")          # 占住句柄，让 os.rename 失败
        try:
            _, undo_report = self.undo(report.batch_id)
        finally:
            handle.close()

        self.assertEqual(undo_report.failed, 1)
        self.assertEqual(undo_report.restored, 1)

        failed = [r for r in undo_report.results if r.status == STATUS_FAILED][0]
        self.assertEqual(failed.src, str(self.root / "照片.jpg"))
        self.assertIn("占用", failed.message)
        self.assertFalse(failed.ok)
        # 注：失败条目的 verified 字段仍是默认 True，语义上有点别扭，
        # 但 UndoReport.unverified 只统计"已恢复且未校验通过"的条目，
        # 所以不会影响任何界面数字（已在报告里记为一处小瑕疵）。

        # 文件还在分类文件夹里，原位不该出现半成品
        self.assertTrue(os.path.isfile(locked_dst))
        self.assertEqual(
            md5(locked_dst), self.expected_md5(["照片.jpg"])["照片.jpg"]
        )
        self.assertFalse(os.path.exists(self.root / "照片.jpg"))

        # 另一个文件正常回位
        self.assertTrue((self.root / "论文.pdf").is_file())
        self.assertFalse(os.path.exists(paper_dst))

        # 记录必须如实写成 failed —— 记成 restored 会让用户以为已经回位
        statuses = {e["src"]: e["status"] for e in self.read_raw() if e.get("undo_of")}
        self.assertEqual(statuses[str(self.root / "照片.jpg")], STATUS_FAILED)
        self.assertEqual(statuses[str(self.root / "论文.pdf")], STATUS_RESTORED)

        # 失败的那个仍然算可撤销：用户关掉占用程序后还能再试
        self.assertEqual(list_batches(RecordStore(self.records_path))[0].undoable, 1)
        self.assertIn("失败 1 个", undo_report.summary_text())


# ====================================================================== 补充：纯逻辑
class TestDataStructures(unittest.TestCase):
    """数据结构的小账要算对（界面全靠这些数字说话）。"""

    def test_undo_report_counters(self):
        report = UndoReport(undo_id="undo-1", batch_id="b1", total=5)
        report.results = [
            UndoItem("a", "a2", "图片", STATUS_RESTORED, "已恢复"),
            UndoItem("b", "b2", "图片", STATUS_RESTORED, "已恢复", verified=False),
            UndoItem("c", "c2", "文档", STATUS_SKIPPED, "跳过"),
            UndoItem("d", "d2", "文档", STATUS_FAILED, "失败"),
        ]
        self.assertEqual(report.restored, 2)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(report.failed, 1)
        self.assertEqual(report.unverified, 1)
        self.assertEqual(report.remaining, 1)
        self.assertIn("成功恢复 2 个", report.summary_text())
        self.assertIn("需人工确认", report.summary_text())

    def test_undo_plan_counters(self):
        plan = UndoPlan(
            batch_id="b1",
            root="R",
            items=[
                UndoItem("a", "a2", "图片", STATUS_READY, "可以恢复"),
                UndoItem("b", "b2", "图片", STATUS_SKIPPED, "跳过"),
            ],
        )
        self.assertEqual(plan.total, 2)
        self.assertEqual(plan.ready_count, 1)
        self.assertEqual(plan.skipped_count, 1)
        self.assertEqual(plan.summary_text(), "可撤销 1 个，不会撤销 1 个")

    def test_batch_summary_label_variants(self):
        # time_text 只取到分钟（[:16]），label 用的是 time_text
        undoable = BatchSummary("b", "2026-10-18 21:30:00", "R", 5, 0, 0, 0, 2)
        self.assertEqual(undoable.time_text, "2026-10-18 21:30")
        self.assertEqual(undoable.label, "2026-10-18 21:30 · 可撤销 2 个")

        uncertain = BatchSummary("b", "2026-10-18 21:30:00", "R", 0, 0, 0, 3, 0)
        self.assertEqual(uncertain.label, "2026-10-18 21:30 · 状态不确定")

        nothing = BatchSummary("b", "2026-10-18 21:30:00", "R", 3, 0, 0, 0, 0)
        self.assertEqual(nothing.label, "2026-10-18 21:30 · 无可撤销文件")

        unknown = BatchSummary("b", "", "R", 0, 1, 1, 0, 0)
        self.assertEqual(unknown.label, "时间未知 · 无可撤销文件")

    def test_undo_item_name_and_ok(self):
        item = UndoItem("C:\\a\\b.txt", "C:\\r\\文档\\b.txt", "文档", STATUS_RESTORED)
        self.assertEqual(item.name, "b.txt")
        self.assertTrue(item.ok)
        self.assertFalse(UndoItem("s", "d", "文档", STATUS_FAILED).ok)


# ====================================================================== 补充：UI
class TestUiImportable(unittest.TestCase):
    """界面不做自动化（Tk 需要真实 mainloop），只确认模块能被正常导入。"""

    def test_history_and_main_window_import(self):
        try:
            import tkinter  # noqa: F401
        except Exception as exc:                 # pragma: no cover
            self.skipTest(f"环境没有可用的 tkinter：{exc}")

        import app.ui.history as history
        import app.ui.main_window as main_window

        self.assertTrue(hasattr(history, "HistoryWindow"))
        self.assertTrue(hasattr(main_window, "MainWindow"))
        # 界面只能用 undoer 的判断结果，不能自己决定动哪个文件
        self.assertTrue(hasattr(history, "build_undo_plan"))
        self.assertTrue(hasattr(history, "execute_undo"))


# ====================================================================== 发布前补充
class TestUndoableFollowsDisk(UndoTempCase):
    """可撤销数必须跟着磁盘现状走，不能只看历史记录。

    发布前检查发现的场景：撤销到一半断电，文件已经被搬回原位、但撤销记录没写成，
    只看记录会显示"可撤销 N 个"，用户点下去却只能得到一堆"目标文件已不存在"。
    """

    def test_restore_without_record_is_not_counted_as_undoable(self):
        report = self.organize(["照片.jpg"])
        dst = Path(self.dst_of(report, "照片.jpg"))
        os.replace(dst, self.root / "照片.jpg")      # 手工完成恢复，不写撤销记录

        summary = list_batches(RecordStore(self.records_path))[0]
        self.assertEqual(summary.moved, 1, "原始记录不变，仍然是 moved")
        self.assertEqual(summary.undoable, 0, "文件已不在目标位置，不该再算可撤销")

        plan = build_undo_plan(RecordStore(self.records_path), report.batch_id)
        self.assertEqual(plan.ready_count, 0)
        self.assertEqual(plan.skipped_count, 1)
        self.assertIn("不存在", plan.items[0].message)

    def test_externally_deleted_file_is_not_counted(self):
        report = self.organize(["照片.jpg"])
        delete_path(self.dst_of(report, "照片.jpg"))

        summary = list_batches(RecordStore(self.records_path))[0]
        self.assertEqual(summary.undoable, 0, "目标文件被外部删除后不该仍显示可撤销")
        self.assertEqual(summary.moved, 1)

    def test_normal_batch_still_counted(self):
        """反向确认：文件都还在时，可撤销数不能被这次改动弄丢。"""
        self.organize(["照片.jpg", "论文.pdf"])
        summary = list_batches(RecordStore(self.records_path))[0]
        self.assertEqual(summary.undoable, 2)



if __name__ == "__main__":
    unittest.main()
