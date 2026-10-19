# -*- coding: utf-8 -*-
"""运行日志：程序出问题时的"黑匣子"。

日志写到 %APPDATA%\\清爽收纳\\logs\\app.log，
不放在程序目录，因为程序可能装在 Program Files 这种没有写权限的地方。
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import APP_NAME

_LOGGER_NAME = "qingcheng"


def get_log_dir() -> Path:
    """返回日志目录，不存在就创建。"""
    base = os.getenv("APPDATA") or str(Path.home())
    log_dir = Path(base) / APP_NAME / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_logger() -> logging.Logger:
    """拿到全局 logger；重复调用不会重复添加 handler。"""
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)

    try:
        handler = RotatingFileHandler(
            get_log_dir() / "app.log",
            maxBytes=1_000_000,      # 单个文件最大 1MB
            backupCount=3,           # 最多保留 3 个历史文件
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)
    except OSError:
        # 万一日志目录也建不了，不能让程序因此崩溃：退化成什么都不记
        logger.addHandler(logging.NullHandler())

    return logger


def log_exception(logger: logging.Logger, exc: Exception) -> None:
    """记录完整堆栈（只进日志，不进界面）。"""
    logger.exception("发生异常：%s", exc)
