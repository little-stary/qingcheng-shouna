# -*- coding: utf-8 -*-
"""操作记录：把每一次移动写进本地 JSONL 文件。

存放位置：
    %APPDATA%\\清爽收纳\\records.jsonl

为什么放这里：
    1. 程序可能装在 Program Files，没有写权限；
    2. 记录里有你的文件路径，属于隐私，不能放进项目目录（会被提交到 GitHub）。

为什么用 JSONL（每行一个 JSON）：
    追加写最安全——写第 100 条时出错，前 99 条不受影响，不会把整个文件写坏。

一次移动写两行（安全审查后新增）：
    第一行 phase="begin"、status="moving" —— **移动之前**先落盘，
        万一移动完程序就被关掉/断电，至少还知道"这个文件本来要搬去哪"；
    第二行 phase="end"、status=moved/skipped/failed —— 移动之后的真实结果。
    read_all() 会把同一批次、同一源文件的两行合并成一行（以后写的为准），
    所以调用方看到的仍然是"一个文件一条记录"。
    如果只剩 begin 没有 end，说明上一次运行中途断了，
    这条记录的 status 会是 "moving"——撤销功能需要按它去磁盘上核实。
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from app.config import APP_NAME

RECORD_FILENAME = "records.jsonl"

# 只写了"准备移动"、没等到结果的状态
STATUS_MOVING = "moving"


class RecordError(Exception):
    """记录写入失败。界面上要明确提示用户，不能悄悄吞掉。"""


def default_record_path() -> Path:
    """默认记录文件位置（跟着用户走，不进项目目录）。"""
    base = os.getenv("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME / RECORD_FILENAME


def now_text() -> str:
    """本地时间，精确到秒。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class RecordStore:
    """操作记录的读写。

    写入失败会抛 RecordError，由调用方决定怎么提示用户——
    这里绝不"静默失败"，否则用户以为有记录、其实没有。
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_record_path()
        # 读取时被跳过的坏行数（断电、写到一半会留下半行）
        self.skipped_lines = 0

    # ------------------------------------------------------------------ 写
    def append(self, entry: Dict) -> None:
        """追加一条记录，立即落盘（flush + fsync），保证断电也不丢。

        安全审查修复：
            上一次如果在写一行的中途断电，文件末尾就没有换行符。
            这时直接追加，新记录会**接在坏行后面**，变成一行无效 JSON，
            等于把下一条记录也一起弄丢了。所以追加前先补一个换行。
        """
        line = json.dumps(entry, ensure_ascii=False)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            need_newline = self._ends_without_newline()
            with open(self.path, "a", encoding="utf-8") as f:
                if need_newline:
                    f.write("\n")           # 把上次残留的半行"封口"
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())        # 真正写到磁盘，而不是只放在缓冲区
        except OSError as exc:
            raise RecordError(f"操作记录写入失败：{exc}") from exc

    def _ends_without_newline(self) -> bool:
        """文件存在、非空、且最后一个字节不是换行 → 上次写坏了。"""
        try:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return False
            with open(self.path, "rb") as f:
                f.seek(-1, os.SEEK_END)
                return f.read(1) != b"\n"
        except OSError:
            # 读不了就按"正常"处理，让后面的写入去暴露真正的问题
            return False

    # ------------------------------------------------------------------ 读
    def read_all(self) -> List[Dict]:
        """读出全部历史记录。文件不存在时返回空列表（第一次使用很正常）。

        同一批次、同一源文件如果既有 begin 又有 end，只保留最后一条。
        """
        self.skipped_lines = 0
        if not self.path.exists():
            return []

        entries: List[Dict] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        # 某一行坏了（比如写到一半断电）不该让整个历史都读不出来
                        self.skipped_lines += 1
                        continue
        except OSError as exc:
            raise RecordError(f"操作记录读取失败：{exc}") from exc

        return self._collapse(entries)

    @staticmethod
    def _collapse(entries: List[Dict]) -> List[Dict]:
        """把"开始/结束"两条合并成一条，保留最后一次写入的内容。

        没有 batch_id 或 src 的记录（手工追加、旧格式）原样保留。
        """
        latest_index: Dict[tuple, int] = {}
        merged: List[Dict] = []

        for entry in entries:
            batch_id = entry.get("batch_id")
            src = entry.get("src")
            if not batch_id or not src:
                merged.append(entry)
                continue

            key = (batch_id, src)
            if key in latest_index:
                merged[latest_index[key]] = entry      # 原地覆盖，顺序不乱
            else:
                latest_index[key] = len(merged)
                merged.append(entry)

        return merged

    def read_batch(self, batch_id: str) -> List[Dict]:
        """按批次号取记录（撤销功能会用到）。"""
        return [e for e in self.read_all() if e.get("batch_id") == batch_id]

    def exists(self) -> bool:
        return self.path.exists()
