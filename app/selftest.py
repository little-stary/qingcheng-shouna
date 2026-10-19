# -*- coding: utf-8 -*-
"""打包自检：在临时目录里完整跑一遍「扫描 → 方案 → 整理 → 撤销」。

用途：
    验证**打包后的 exe** 仍能正常读写文件、写操作记录、撤销。
    exe 是窗口程序（没有控制台），所以结果同时写入
    `%TEMP%\\qingcheng_selftest_report.txt`。

用法：
    清爽收纳.exe --selftest
    退出码 0 = 全部通过，1 = 有失败。

安全说明：
    全程只在自己的临时目录里操作，结束自动清理；不读取、不修改任何用户文件。
"""

import hashlib
import os
import tempfile
import traceback
from pathlib import Path
from typing import List, Tuple

from app.core.mover import execute_plan
from app.core.planner import build_plan
from app.core.records import RecordStore
from app.core.scanner import scan_folder
from app.core.undoer import build_undo_plan, execute_undo, list_batches

REPORT_NAME = "qingcheng_selftest_report.txt"

# 故意包含中文、空格和特殊字符，验证打包后路径处理没问题
SAMPLE_FILES = {
    "校园 风景 #1.jpg": b"jpg-bytes-0123456789",
    "实验报告（终版）.pdf": b"pdf-bytes-0123456789",
    "课程录像.mp4": b"mp4-bytes-0123456789",
}


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(text: str) -> Path:
    target = Path(tempfile.gettempdir()) / REPORT_NAME
    with open(target, "w", encoding="utf-8") as f:
        f.write(text)
    return target


def run() -> Tuple[bool, str]:
    """执行自检，返回 (是否全部通过, 报告文本)。"""
    lines: List[str] = []
    ok = True

    def check(name: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "PASS" if condition else "FAIL"
        if not condition:
            ok = False
        lines.append(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    try:
        with tempfile.TemporaryDirectory(prefix="qingcheng_selftest_") as tmp:
            base = Path(tmp)
            work = base / "下载"
            work.mkdir()
            records = base / "records.jsonl"
            store = RecordStore(records)

            for name, data in SAMPLE_FILES.items():
                with open(work / name, "wb") as f:
                    f.write(data)
            before = {name: _md5(work / name) for name in SAMPLE_FILES}
            check("创建测试文件", len(before) == 3, str(work))

            scan = scan_folder(str(work))
            check("扫描第一层", scan.total_files == 3, f"找到 {scan.total_files} 个")

            plan = build_plan(scan.files, str(work))
            check("生成整理方案", plan.total == 3, f"方案 {plan.total} 条")

            move_report = execute_plan(plan, store=store)
            check("执行整理", move_report.moved == 3, move_report.summary_text())
            check("分类文件夹已创建", (work / "图片").is_dir() and (work / "文档").is_dir())

            batches = list_batches(store)
            check("读取历史批次", len(batches) == 1 and batches[0].undoable == 3,
                  batches[0].label if batches else "没有批次")

            undo_plan = build_undo_plan(store, batches[0].batch_id)
            undo_report = execute_undo(undo_plan, store=store)
            check("执行撤销", undo_report.restored == 3, undo_report.summary_text())

            after = {name: _md5(work / name) for name in SAMPLE_FILES}
            check("文件回到原位置", all((work / n).is_file() for n in SAMPLE_FILES))
            check("内容与整理前一致", after == before)

            records_text = ""
            if records.exists():
                with open(records, "r", encoding="utf-8") as f:
                    records_text = f.read()
            check("操作记录可写入", '"undo_of"' in records_text and len(records_text) > 0,
                  f"{len(records_text)} 字符")
            check("撤销后无可撤销项", list_batches(store)[0].undoable == 0)

    except Exception:                       # noqa: BLE001 —— 自检要把错误变成报告，而不是弹窗
        ok = False
        lines.append("[FAIL] 自检过程抛出异常：")
        lines.append(traceback.format_exc())

    header = "SELFTEST OK" if ok else "SELFTEST FAILED"
    text = "\n".join([header, "-" * 50] + lines + ["-" * 50, f"Python: {os.sys.version.split()[0]}"])
    try:
        path = _write_report(text)
        text += f"\n报告文件：{path}"
    except OSError:
        pass
    return ok, text
