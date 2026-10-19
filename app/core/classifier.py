# -*- coding: utf-8 -*-
"""分类逻辑：给一个文件名，判断它属于哪一类。

这个模块不碰文件系统，只做纯计算，所以特别适合写单元测试。
"""

import os

from app.config import EXT_MAP, FALLBACK_CATEGORY


def get_ext(filename: str) -> str:
    """取小写扩展名（带点）。没有扩展名时返回空字符串。

    例：
        报告.PDF      -> ".pdf"
        我的照片       -> ""
        .gitignore    -> ""      （只有点、没有名字，不算扩展名）
    """
    name = os.path.basename(filename)
    ext = os.path.splitext(name)[1].lower()

    # splitext(".gitignore") 会返回 (".gitignore", "")，这里刚好是空字符串
    return ext


def classify(filename: str) -> str:
    """按扩展名判断分类，判断不出来的一律归到「其他」。"""
    ext = get_ext(filename)
    return EXT_MAP.get(ext, FALLBACK_CATEGORY)
