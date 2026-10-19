# 第三方组件与许可证

「清爽收纳」本身以 **MIT License** 发布（见 [LICENSE](LICENSE)）。
打包后的程序还包含以下第三方组件，它们的许可证均与 MIT 兼容：

| 组件 | 版本 | 许可证 | 用途 |
| --- | --- | --- | --- |
| customtkinter | 6.0.0 | CC0-1.0 | 界面控件库 |
| darkdetect | 0.8.0 | BSD-3-Clause | customtkinter 的依赖（检测系统深色模式） |
| packaging | 26.3 | Apache-2.0 或 BSD-2-Clause | customtkinter 的依赖（版本解析） |
| Python | 3.14.x | PSF License | 程序运行时（PyInstaller 打包时一并分发） |
| Tcl/Tk | 8.6 / 9.x | BSD 风格 | tkinter 的底层图形库 |
| PyInstaller bootloader | 6.22.3 | GPL-2.0-or-later + Bootloader Exception | 启动器（由打包工具嵌入） |

许可证全文放在 [`licenses/`](licenses/) 目录：

- `customtkinter-CC0-1.0.txt`
- `darkdetect-BSD-3-Clause.txt`
- `packaging-Apache-2.0.txt` / `packaging-BSD-2-Clause.txt`

## 兼容性结论

- **CC0-1.0**：公共领域奉献，无附加义务。
- **BSD-3-Clause**：再分发时需保留版权声明与许可证全文（已随包提供）。
- **Apache-2.0**：需保留许可证与声明；本项目未修改其源码。
- **PSF License / Tcl-Tk**：允许再分发，需保留声明。
- **PyInstaller Bootloader Exception**：明确允许用 PyInstaller 打包的程序以任意许可证发布，
  不会让本项目变成 GPL。

## 分发时的义务

1. 发布包中保留本文件、`LICENSE` 与 `licenses/` 目录（`packaging/build_exe.bat` 会自动复制）。
2. 不得删除或修改第三方版权声明。
3. 如果以后新增依赖，请先确认其许可证与 MIT 兼容，并把许可证全文补进 `licenses/`。
