# -*- coding: utf-8 -*-
"""扫描器：读取文件夹第一层的文件信息。

安全承诺（第一阶段就定下的，一直有效）：
    本模块只调用 os.scandir 和元数据读取，
    **没有任何** 创建、写入、移动、重命名、删除文件的代码。

第三阶段新增：
    on_progress(count)  —— 每扫一批回报一次进度，界面可以显示"已找到 N 个"
    should_cancel()     —— 每扫一批问一次"要不要停"，用户可以中途取消
"""

import os
from pathlib import Path
from typing import Callable, Optional

from app.config import MAX_SCAN_FILES
from app.core.classifier import classify
from app.core.models import FileItem, ScanResult

# 每处理多少个条目回报一次进度 / 检查一次取消
PROGRESS_STEP = 200


def scan_folder(
    folder: str,
    max_files: int = MAX_SCAN_FILES,
    on_progress: Optional[Callable[[int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """扫描 folder 的第一层（不进入子文件夹），返回统计结果。

    参数：
        folder        —— 要扫描的文件夹路径
        max_files     —— 最多收集多少个文件，防止误选大盘根目录时卡死
        on_progress   —— 进度回调，参数是"目前已收集的文件数"
        should_cancel —— 返回 True 表示用户要求停止，扫描会带着已有结果返回

    异常：
        文件夹不存在 / 不是文件夹 / 没有读取权限时，抛出异常由上层翻译成中文提示。
    """

    root = Path(folder)

    # 先做三次检查，把"明显不对"的情况挡在门外
    if not root.exists():
        raise FileNotFoundError(f"文件夹不存在：{root}")
    if not root.is_dir():
        raise NotADirectoryError(f"这不是一个文件夹：{root}")

    result = ScanResult(folder=str(root))
    seen = 0                    # 遍历过多少个条目（含子文件夹）

    # os.scandir 比 os.listdir 快，因为它能直接拿到"是不是文件夹""大小"等信息
    # 用 with 保证用完自动关闭句柄，不会占住文件夹
    with os.scandir(root) as entries:
        for entry in entries:
            seen += 1

            # 先看要不要停：用户点了取消就带着已有结果返回
            if should_cancel is not None and seen % PROGRESS_STEP == 0 and should_cancel():
                result.cancelled = True
                break

            try:
                if entry.is_dir(follow_symlinks=False):
                    # 子文件夹只计数，绝对不进去
                    result.sub_folder_count += 1
                    continue

                if not entry.is_file(follow_symlinks=False):
                    # 快捷方式、设备文件等，跳过
                    continue

                info = entry.stat(follow_symlinks=False)
                result.files.append(
                    FileItem(
                        path=entry.path,
                        name=entry.name,
                        ext=os.path.splitext(entry.name)[1].lower(),
                        size=info.st_size,
                        mtime=info.st_mtime,
                        category=classify(entry.name),
                    )
                )

                if len(result.files) >= max_files:
                    result.truncated = True
                    break

            except (PermissionError, OSError):
                # 单个文件读不到（被占用、权限不足、名字过长）不该让整次扫描失败
                result.skipped.append(entry.name)
                continue

            finally:
                # 进度回调放在 finally 里，保证出错时界面也能继续更新
                if on_progress is not None and seen % PROGRESS_STEP == 0:
                    on_progress(len(result.files))

    return result


def format_size(num_bytes: int) -> str:
    """把字节数变成人看得懂的大小：1536 -> 1.5 KB。"""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
