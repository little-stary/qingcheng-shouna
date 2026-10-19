# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：清爽收纳（单文件版）。

用法（在项目根目录，或直接跑 packaging\build_exe.bat）：
    .venv\Scripts\pyinstaller.exe packaging\qingcheng-shouna.spec --noconfirm --clean

产物：
    dist\清爽收纳.exe
"""

import os

from PyInstaller.utils.hooks import collect_data_files

# 项目根目录（本 spec 位于 packaging/ 下，往上跳一级）
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

# customtkinter 的主题 json 等资源必须带上，否则界面会报找不到资源
datas = collect_data_files("customtkinter")

# 许可证与第三方声明：随程序一起分发
datas += [
    (os.path.join(ROOT, "LICENSE"), "."),
    (os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), "."),
    (os.path.join(ROOT, "licenses"), "licenses"),
]

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 只排除测试相关模块；不要排除 unittest（部分库运行时会动态引用）
    excludes=["tests", "tkinter.test", "test", "pydoc", "doctest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

# 单文件模式：所有内容打进一个 exe，运行时解压到临时目录。
# 已实测：tkinter、customtkinter 主题资源、中文路径、操作记录读写均正常。
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="清爽收纳",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,            # 双击运行不弹黑色控制台窗口
    disable_windowed_traceback=False,
    icon=None,                # 还没有应用图标，发布前可补一个 .ico
)
