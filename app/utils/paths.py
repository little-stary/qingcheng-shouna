# -*- coding: utf-8 -*-
"""路径小工具：显示用的短路径、目标路径合法性检查。

第一阶段就约定过：只能操作"用户选中的目录内部"。
这里把这条约定做成可测试的函数，而不是散落在各处 if 里。

安全审查（第四阶段后）补充：
    原来用 abspath 比较，只处理了 ".." 和大小写，**没有处理符号链接/目录联接**。
    Windows 上一个 mklink /J 建出来的联接，abspath 看起来还在目录里，
    实际写进去的文件会落到目录外面。所以统一改用 realpath（会解析链接）。
"""

import os


def shorten_path(path: str, max_chars: int = 46) -> str:
    """把过长的路径缩成 "D:\\下载\\…\\子目录\\文件.pdf" 的形式。

    只影响显示，不影响真实路径。
    """
    if len(path) <= max_chars:
        return path

    # 保留盘符/根 和 末尾一段，中间的省略
    drive, rest = os.path.splitdrive(path)
    keep_tail = max_chars - len(drive) - 6
    if keep_tail <= 0:
        return path[: max_chars - 1] + "…"
    return f"{drive}\\…{rest[-keep_tail:]}"


def _real(path: str) -> str:
    """转成"真实、绝对、大小写归一"的路径：符号链接和目录联接都会被解析掉。"""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def is_inside(base_dir: str, target: str) -> bool:
    """判断 target 是否在 base_dir 内部（不含 base_dir 本身）。

    用来兜底：任何要写出去的位置，都必须落在用户选中的目录里。
    · Windows 路径大小写不敏感 → 统一 normcase；
    · 目录联接 / 符号链接可能指向外面 → 统一 realpath 解析后再比。
    """
    if not base_dir or not target:
        return False

    base = _real(base_dir)
    dest = _real(target)

    if base == dest:
        return False

    try:
        return os.path.commonpath([base, dest]) == base
    except ValueError:
        # 不同盘符时 commonpath 会抛 ValueError，说明不在同一个根下
        return False


def is_direct_child(parent: str, path: str) -> bool:
    """判断 path 是不是 parent 的直接子项（只差一层，不递归）。

    用于确认「分类文件夹」确实建在所选目录下面，
    而不是被一个同名联接指到了别的地方。
    """
    if not parent or not path:
        return False

    parent_real = _real(parent)
    path_real = _real(path)

    if parent_real == path_real:
        return False

    return os.path.dirname(path_real) == parent_real
