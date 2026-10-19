# -*- coding: utf-8 -*-
"""清爽收纳 · 程序入口。

双击运行、或在项目目录执行：
    python main.py
"""

import sys
import tkinter.messagebox as messagebox

from app.config import APP_NAME
from app.ui.main_window import MainWindow
from app.utils.logger import get_logger


def _install_global_exception_hook() -> None:
    """兜底：任何没被捕获的异常，都记日志 + 弹一句中文提示，而不是直接崩溃。"""

    def handle(exc_type, exc_value, exc_tb):
        logger = get_logger()
        logger.error("未捕获的异常", exc_info=(exc_type, exc_value, exc_tb))
        try:
            messagebox.showerror(
                APP_NAME,
                "程序遇到一个意外问题，已经记录到日志。\n"
                "可以关掉重开再试一次；如果反复出现，请把日志发给开发者。",
            )
        except Exception:          # noqa: BLE001 —— 连弹窗都失败时只能放弃提示
            pass

    sys.excepthook = handle


def main() -> None:
    _install_global_exception_hook()
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    # 自检模式：打包产物验证用。窗口版 exe 没有控制台，
    # 所以结果写到 %TEMP%\qingcheng_selftest_report.txt，退出码表示成败。
    if "--selftest" in sys.argv[1:]:
        from app.selftest import run

        ok, text = run()
        if sys.stdout is not None:
            print(text)
        raise SystemExit(0 if ok else 1)

    main()
