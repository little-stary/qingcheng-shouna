# -*- coding: utf-8 -*-
"""错误翻译：把 Python 的技术异常，变成用户看得懂的中文。

原则：技术细节只写进日志，界面上只出现"人话"。
"""


def friendly_message(exc: Exception) -> str:
    """把异常翻译成一句中文提示。"""

    if isinstance(exc, FileNotFoundError):
        return "这个文件夹不存在，可能已经被移动或删除。请重新选择。"

    if isinstance(exc, NotADirectoryError):
        return "选中的不是文件夹。请重新选择一个文件夹。"

    if isinstance(exc, PermissionError):
        return "没有权限读取这个文件夹。可以换一个文件夹，或用管理员身份重新打开本程序。"

    if isinstance(exc, OSError):
        # Windows 上磁盘空间不足、路径过长等都会以 OSError 形式出现
        text = str(exc).lower()
        if "no space" in text or "磁盘空间" in text:
            return "磁盘空间不足，请先清理一些文件后再试。"
        if "too long" in text or "文件名" in text:
            return "文件路径太长，Windows 无法处理。请把文件夹移到更浅的目录再试。"
        return "读取文件夹时出错。可以换一个文件夹试试，详细信息已记录到日志。"

    return "出了点意外情况，详细信息已记录到日志。"
