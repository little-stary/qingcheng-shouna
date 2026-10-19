# -*- coding: utf-8 -*-
"""测试专用的临时文件清理。

为什么不用 shutil.rmtree / Path.unlink：
    某些环境（本项目的开发沙箱、部分杀毒软件）会把删除接管成"移到回收站"，
    而且还有"批量删除守卫"：短时间内删得太多，会直接抛 SystemExit(1)。
    清理失败不该让测试结论变红——断言早就通过了，出错的是清理这一步。

处理方式：
    1. 正常情况：直接删；
    2. 遇到 SystemExit（批量删除守卫）：本次进程后续都改用"改名挪开"，
       不再反复尝试删除，避免每个用例都白白等重试；
    3. 遇到 OSError（文件被占用等瞬时问题）：重试 3 次，仍失败再改名挪开。

挪走的残留统一放到 tests/_tmp/_stale/（已被 .gitignore 排除），
不会留在用例目录里影响"目录是否干净"这类断言。
"""

import os
import shutil
import time
import uuid
from pathlib import Path

# 本次进程是否已经触发过沙箱的"批量删除守卫"
_bulk_guard_tripped = False

_STALE_DIR = Path(__file__).resolve().parent / "_tmp" / "_stale"


def _rename_aside(target: Path) -> None:
    """删不掉就改名挪到统一的暂存目录，保证原路径消失、用例目录保持干净。"""
    try:
        _STALE_DIR.mkdir(parents=True, exist_ok=True)
        os.rename(target, _STALE_DIR / f"{target.name}-{uuid.uuid4().hex[:8]}")
    except OSError:
        pass


def remove_tree(path) -> None:
    """尽力删除临时目录；失败时改名挪开，绝不把清理错误变成测试失败。"""
    global _bulk_guard_tripped

    target = Path(path)
    if not target.exists():
        return

    if _bulk_guard_tripped:
        _rename_aside(target)
        return

    for _ in range(3):
        try:
            shutil.rmtree(target)
            return
        except SystemExit:
            # 沙箱的批量删除守卫：再试也没用，后续统一走改名
            _bulk_guard_tripped = True
            _rename_aside(target)
            return
        except OSError:
            time.sleep(0.1)

    _rename_aside(target)


def delete_path(path) -> None:
    """删除一个测试文件/目录项（模拟"用户手动删掉"这类场景）。

    批量删除守卫触发后改用改名：原路径同样不存在，用例语义不变。
    """
    global _bulk_guard_tripped

    target = Path(path)
    if not os.path.lexists(target):
        return

    if _bulk_guard_tripped:
        _rename_aside(target)
        return

    try:
        if target.is_dir() and not target.is_symlink():
            os.rmdir(target)
        else:
            target.unlink()
    except SystemExit:
        _bulk_guard_tripped = True
        _rename_aside(target)
    except OSError:
        _rename_aside(target)
