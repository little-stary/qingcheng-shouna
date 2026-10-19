# -*- coding: utf-8 -*-
"""安全护栏测试：用扫描源码的方式守住几条不能破的规矩。

第四阶段开始允许"移动文件"，所以规则从"谁都不许动"收紧成：
    · 任何模块都不许删除文件（remove / unlink / rmdir / rmtree）；
    · 只有 mover.py 允许移动/改名，其它模块一律不许；
    · 任何模块都不许直接写用户文件；
    · 记录只允许写进 records.py 指定的那个 JSONL 文件。

这样以后谁不小心把"顺手删一下"的代码写进来，测试会立刻变红。
"""

import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# 绝对禁止：删文件
FORBIDDEN_EVERYWHERE = [
    "os.remove(",
    "os.unlink(",
    "os.rmdir(",
    "shutil.rmtree",
]

# 只有 mover.py 可以移动/复制文件
FORBIDDEN_OUTSIDE_MOVER = [
    "shutil.move",
    "shutil.copy",
    "os.rename(",
    "os.replace(",
]

# 任何模块都不该直接写用户文件（记录文件由 records.py 用 open 追加）
FORBIDDEN_WRITES = [
    ".write_bytes(",
    "open(..., 'w')",
]

MOVER_MODULE = "mover.py"


def iter_sources():
    for path in APP_DIR.rglob("*.py"):
        yield path, path.read_text(encoding="utf-8")


class TestSafetyRules(unittest.TestCase):
    def test_nothing_deletes_files(self):
        offenders = []
        for path, text in iter_sources():
            for bad in FORBIDDEN_EVERYWHERE:
                if bad in text:
                    offenders.append(f"{path.name}: {bad}")
        self.assertEqual(offenders, [], f"发现删除文件的调用：{offenders}")

    def test_move_only_in_mover(self):
        offenders = []
        for path, text in iter_sources():
            if path.name == MOVER_MODULE:
                continue
            for bad in FORBIDDEN_OUTSIDE_MOVER:
                if bad in text:
                    offenders.append(f"{path.name}: {bad}")
        self.assertEqual(offenders, [], f"移动文件的代码只能写在 {MOVER_MODULE} 里：{offenders}")

    def test_no_direct_file_writes(self):
        offenders = []
        for path, text in iter_sources():
            for bad in FORBIDDEN_WRITES:
                if bad in text:
                    offenders.append(f"{path.name}: {bad}")
        self.assertEqual(offenders, [], f"发现直接写文件：{offenders}")

    def test_mover_checks_conflict_before_move(self):
        """mover 里必须先做检查，再执行移动——顺序不能反。"""
        text = (APP_DIR / "core" / MOVER_MODULE).read_text(encoding="utf-8")

        inside_pos = text.find("if not item.inside or not is_inside(source_dir, item.dst):")
        src_pos = text.find("if not is_inside(source_dir, item.src):")
        check_pos = text.find("if os.path.lexists(item.dst):")
        move_pos = text.find("_move_file(item.src, item.dst)")

        self.assertGreater(inside_pos, 0, "缺少目标越界检查")
        self.assertGreater(src_pos, 0, "缺少源路径越界检查")
        self.assertGreater(check_pos, 0, "缺少目标存在性检查")
        self.assertGreater(move_pos, 0, "缺少实际的移动调用")
        self.assertLess(inside_pos, move_pos, "必须先检查目标越界")
        self.assertLess(src_pos, move_pos, "必须先检查源文件是否在所选目录内")
        self.assertLess(check_pos, move_pos, "必须先检查再移动，顺序反了会覆盖文件")

    def test_move_uses_atomic_rename(self):
        """真正动手的地方必须是 os.rename（Windows 上目标存在会直接失败），
        不能换成会静默覆盖的实现。"""
        text = (APP_DIR / "core" / MOVER_MODULE).read_text(encoding="utf-8")
        start = text.find("def _move_file(")
        self.assertGreater(start, 0, "找不到 _move_file")
        body = text[start:start + 1200]
        self.assertIn("os.rename(src, dst)", body)
        self.assertNotIn("shutil.move(", text,
                         "shutil.move 在目标为文件夹时会改变实际落点")
        self.assertNotIn("shutil.copy(", text)

    def test_records_never_committed(self):
        """记录文件里有个人文件路径，必须被 .gitignore 挡住。"""
        root = Path(__file__).resolve().parent.parent
        ignore = (root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("records.jsonl", ignore)
        self.assertIn("tests/_tmp/", ignore)


if __name__ == "__main__":
    unittest.main()
