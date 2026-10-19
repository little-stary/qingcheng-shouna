# -*- coding: utf-8 -*-
"""主窗口：选文件夹 → 扫描统计 → 生成整理方案并预览。

界面从上到下四块：
    标题   →   选文件夹   →   扫描统计   →   整理预览
底部是状态提示和「停止 / 重新扫描」按钮。

安全边界：
    本文件只负责"读"（扫描、算方案、画界面）。
    真正移动文件的代码要到下一阶段才写，而且必须经过用户二次确认。
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk

from app.config import (
    APP_NAME,
    APP_SLOGAN,
    APP_VERSION,
    CATEGORY_ORDER,
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_PRIMARY,
    COLOR_PRIMARY_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_WEAK,
)
from app.core.models import ScanResult
from app.core.mover import MoveReport, execute_plan
from app.core.records import RecordStore
from app.core.planner import Plan, build_plan
from app.core.scanner import format_size, scan_folder
from app.ui.history import HistoryWindow
from app.ui.layout import clamp_minsize, compute_geometry
from app.ui.preview import PreviewPanel
from app.ui.theme import GAP_CARD, font
from app.ui.widgets import Card, CategoryTile
from app.utils.errors import friendly_message
from app.utils.logger import get_logger, log_exception
from app.utils.paths import shorten_path


class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()

        ctk.set_appearance_mode("light")     # 浅色界面（需求：浅色）
        self.title(f"{APP_NAME}  v{APP_VERSION}")

        self._apply_window_geometry()
        self.configure(fg_color=COLOR_BG)

        self.logger = get_logger()
        self.folder = ""                 # 当前选中的文件夹
        self.result: ScanResult | None = None
        self.plan: Plan | None = None
        self.is_scanning = False
        self._scan_cancel = threading.Event()
        self._scan_found = 0             # 扫描进度（已找到多少个文件）
        self.is_moving = False
        self._move_cancel = threading.Event()
        self._move_done = 0
        self._move_total = 0
        self._history_win = None         # 历史记录窗口只允许开一个，避免两个窗口同时撤销

        self._build_ui()

    # ------------------------------------------------------------------ 窗口
    def _apply_window_geometry(self) -> None:
        """按当前屏幕（逻辑像素）算窗口大小，避免在 125% / 150% 缩放下超出屏幕。

        Tk 的 winfo_screenwidth/height 返回的已经是折算后的逻辑尺寸，
        所以这里不需要再乘缩放比例，乘了反而会错。
        """
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        self.geometry(compute_geometry(screen_w, screen_h))
        self.minsize(*clamp_minsize(screen_w, screen_h))

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        root = ctk.CTkFrame(self, fg_color="transparent")
        root.pack(fill="both", expand=True, padx=24, pady=14)

        # 打包顺序很关键：
        # 先把标题、选文件夹、统计、页脚按顺序放好，页脚用 side="bottom" 钉在底部，
        # 最后再放预览区（expand=True），窗口变矮时被压缩的是预览列表，按钮不会消失。
        self._build_header(root)
        self._build_folder_card(root)
        self._build_stats_card(root)
        self._build_footer(root)
        self._build_preview(root)

    def _build_header(self, parent) -> None:
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x")

        ctk.CTkLabel(
            header, text=APP_NAME, font=font(22, bold=True), text_color=COLOR_TEXT
        ).pack(anchor="w")

        ctk.CTkLabel(
            header, text=APP_SLOGAN, font=font(11), text_color=COLOR_TEXT_WEAK
        ).pack(anchor="w", pady=(2, 0))

    def _build_folder_card(self, parent) -> None:
        card = Card(parent, "第 1 步 · 选择文件夹")
        card.pack(fill="x", pady=(14, 0))

        row = ctk.CTkFrame(card.inner, fg_color="transparent")
        row.pack(fill="x")

        self.path_var = tk.StringVar(value="")
        self.path_entry = ctk.CTkEntry(
            row,
            textvariable=self.path_var,
            state="readonly",
            height=36,
            font=font(12),
            fg_color="#F8FAFC",
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            placeholder_text="还没有选择文件夹",
        )
        self.path_entry.pack(side="left", fill="x", expand=True)

        # 路径太长时会在中间省略；右键输入框可以复制完整路径
        self.path_entry.bind("<Button-3>", self._copy_path)

        self.choose_btn = ctk.CTkButton(
            row,
            text="选择文件夹",
            width=116,
            height=36,
            font=font(13, bold=True),
            fg_color=COLOR_PRIMARY,
            hover_color=COLOR_PRIMARY_HOVER,
            corner_radius=9,
            command=self.on_choose_folder,
        )
        self.choose_btn.pack(side="left", padx=(10, 0))

        ctk.CTkLabel(
            card.inner,
            text="只扫描第一层，子文件夹不读取；全程只读，不改动文件。（右键路径可复制）",
            font=font(10),
            text_color=COLOR_TEXT_WEAK,
            anchor="w",
        ).pack(fill="x", pady=(7, 0))

    def _build_stats_card(self, parent) -> None:
        card = Card(parent, "第 2 步 · 扫描结果")
        card.pack(fill="x", pady=(GAP_CARD, 0))

        self.summary_label = ctk.CTkLabel(
            card.inner,
            text="选择文件夹后会自动扫描。",
            font=font(12),
            text_color=COLOR_TEXT_WEAK,
            anchor="w",
        )
        self.summary_label.pack(fill="x", pady=(0, 8))

        grid = ctk.CTkFrame(card.inner, fg_color="transparent")
        grid.pack(fill="x")

        self.tiles: dict[str, CategoryTile] = {}
        for index, name in enumerate(CATEGORY_ORDER):
            row, col = divmod(index, 6)
            tile = CategoryTile(grid, name)
            tile.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            self.tiles[name] = tile

        for col in range(6):
            grid.grid_columnconfigure(col, weight=1)

    def _build_preview(self, parent) -> None:
        self.preview = PreviewPanel(
            parent,
            on_generate=self.on_generate_plan,
            on_cancel=self.on_cancel_plan,
            on_confirm=self.on_confirm_plan,
            on_stop=self.on_stop_move,
        )
        self.preview.pack(fill="both", expand=True, pady=(GAP_CARD, 0))

    def _build_footer(self, parent) -> None:
        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.pack(side="bottom", fill="x", pady=(GAP_CARD, 0))

        self.status_label = ctk.CTkLabel(
            footer, text="就绪", font=font(11), text_color=COLOR_TEXT_WEAK, anchor="w"
        )
        self.status_label.pack(side="left")

        self.rescan_btn = ctk.CTkButton(
            footer,
            text="重新扫描",
            width=104,
            height=32,
            font=font(11),
            fg_color=COLOR_CARD,
            hover_color="#F1F4F8",
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            corner_radius=9,
            state="disabled",
            command=self.on_rescan,
        )
        self.rescan_btn.pack(side="right")

        # 「历史记录」放在「重新扫描」左边的视觉位置：
        # 先 pack 的在最右边，所以后 pack 的这个会出现在 rescan 左侧。
        self.history_btn = ctk.CTkButton(
            footer,
            text="历史记录",
            width=88,
            height=32,
            font=font(11),
            fg_color=COLOR_CARD,
            hover_color="#F1F4F8",
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            corner_radius=9,
            command=self.on_open_history,
        )
        self.history_btn.pack(side="right", padx=(0, 6))

        # 扫描时才显示，避免一直占着位置
        self.stop_btn = ctk.CTkButton(
            footer,
            text="停止扫描",
            width=104,
            height=32,
            font=font(11),
            fg_color=COLOR_CARD,
            hover_color="#F1F4F8",
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            corner_radius=9,
            command=self.on_stop_scan,
        )

    # ------------------------------------------------------------------ 选择与扫描
    def on_choose_folder(self) -> None:
        if self.is_scanning:
            return

        folder = filedialog.askdirectory(title="选择要整理的文件夹")
        if not folder:
            return                      # 用户点了取消，什么都不做

        # Windows 返回的是正斜杠，转成反斜杠更好看；
        # abspath 保证后续扫描、方案、记录里用的都是绝对路径
        self.folder = os.path.abspath(folder.replace("/", "\\"))
        self.path_var.set(shorten_path(self.folder, 58))
        self.plan = None
        self.preview.show_idle()
        self.preview.set_can_generate(False)
        self.start_scan()

    def on_rescan(self) -> None:
        if self.folder and not self.is_scanning:
            self.plan = None
            self.preview.show_idle()
            self.preview.set_can_generate(False)
            self.start_scan()

    def on_stop_scan(self) -> None:
        if self.is_scanning:
            self._scan_cancel.set()
            self.status_label.configure(text="正在停止…")

    def start_scan(self) -> None:
        """启动扫描。放到后台线程，避免大文件夹把界面卡成"未响应"。"""
        self.is_scanning = True
        self._scan_cancel.clear()
        self._scan_found = 0

        self._set_scan_buttons(scanning=True)
        self.status_label.configure(text="正在扫描…")
        self.summary_label.configure(text="正在读取文件信息，请稍候…")
        for tile in self.tiles.values():
            tile.update_value(0, "")

        thread = threading.Thread(target=self._scan_worker, args=(self.folder,), daemon=True)
        thread.start()
        self._poll_scan_progress()

    def _scan_worker(self, folder: str) -> None:
        """后台线程：只做扫描，绝对不碰界面控件。"""
        try:
            result = scan_folder(
                folder,
                on_progress=self._on_scan_progress,
                should_cancel=self._scan_cancel.is_set,
            )
        except Exception as exc:                      # noqa: BLE001 —— 任何异常都要变成中文提示
            log_exception(self.logger, exc)
            message = friendly_message(exc)
            self.after(0, lambda: self._on_scan_failed(message))
        else:
            # 界面更新必须回到主线程，用 after 排进队列
            self.after(0, lambda: self._on_scan_done(result))

    def _on_scan_progress(self, count: int) -> None:
        """进度回调（在后台线程执行）：只记数字，不碰界面。"""
        self._scan_found = count

    def _poll_scan_progress(self) -> None:
        """在主线程里定时把"已找到多少个"刷到状态栏。"""
        if not self.is_scanning:
            return
        if self._scan_found:
            self.status_label.configure(text=f"正在扫描… 已找到 {self._scan_found} 个文件")
        self.after(300, self._poll_scan_progress)

    def _on_scan_done(self, result: ScanResult) -> None:
        self.result = result
        self.is_scanning = False
        self._set_scan_buttons(scanning=False)

        counts = result.count_by_category()
        sizes = result.size_by_category()
        for name, tile in self.tiles.items():
            tile.update_value(counts.get(name, 0), format_size(sizes.get(name, 0)))

        if result.cancelled:
            self.summary_label.configure(
                text=f"扫描已停止，已找到 {result.total_files} 个文件"
            )
            self.status_label.configure(text="扫描已停止")
            self.preview.show_idle("扫描被中止，可以重新扫描，或直接生成方案。")
            self.preview.set_can_generate(result.total_files > 0)
            return

        if result.total_files == 0:
            hint = "这个文件夹的第一层没有可整理的文件"
            if result.sub_folder_count:
                hint += f"（只有 {result.sub_folder_count} 个子文件夹，不会进入）"
            self.summary_label.configure(text=hint)
            self.status_label.configure(text="扫描完成 · 空文件夹")
            self.preview.show_idle("没有可整理的文件，无需生成方案。")
            self.preview.set_can_generate(False)
            return

        parts = [f"共 {result.total_files} 个文件", format_size(result.total_size)]
        if result.sub_folder_count:
            parts.append(f"子文件夹 {result.sub_folder_count} 个（不会整理）")
        if result.skipped:
            parts.append(f"{len(result.skipped)} 个文件读不到已跳过")
        if result.truncated:
            parts.append("文件太多，只统计了前 20000 个")

        self.summary_label.configure(text=" · ".join(parts))
        self.status_label.configure(text="扫描完成")
        self.preview.show_idle(f"共 {result.total_files} 个文件，点「生成整理方案」查看预览。")
        self.preview.set_can_generate(True)

    def _on_scan_failed(self, message: str) -> None:
        self.is_scanning = False
        self._set_scan_buttons(scanning=False)
        self.result = None
        self.summary_label.configure(text=message)
        self.status_label.configure(text="扫描失败")
        self.preview.show_idle("扫描失败，先选一个可读的文件夹吧。")
        self.preview.set_can_generate(False)

    def _set_scan_buttons(self, scanning: bool) -> None:
        self.choose_btn.configure(state="disabled" if scanning else "normal")
        if scanning:
            self.rescan_btn.pack_forget()
            self.stop_btn.pack(side="right", padx=(0, 6))
        else:
            self.stop_btn.pack_forget()
            self.rescan_btn.pack(side="right")
            self.rescan_btn.configure(state="normal" if self.folder else "disabled")

    # ------------------------------------------------------------------ 方案
    def on_generate_plan(self) -> None:
        """生成整理方案。只做计算，不动任何文件。"""
        if self.is_scanning or not self.result or not self.result.files:
            return

        self.preview.show_loading()
        self.preview.set_can_generate(False)
        self.status_label.configure(text="正在生成整理方案…")

        files = list(self.result.files)
        folder = self.folder
        thread = threading.Thread(target=self._plan_worker, args=(files, folder), daemon=True)
        thread.start()

    def _plan_worker(self, files, folder: str) -> None:
        try:
            plan = build_plan(files, folder)
        except Exception as exc:                  # noqa: BLE001
            log_exception(self.logger, exc)
            message = friendly_message(exc)
            self.after(0, lambda: self._on_plan_failed(message))
        else:
            self.after(0, lambda: self._on_plan_done(plan))

    def _on_plan_done(self, plan: Plan) -> None:
        self.plan = plan
        self.preview.show_plan(plan)
        self.preview.set_can_generate(True)

        status = f"方案已生成 · {plan.total} 个文件"
        if plan.conflict_count:
            status += f"，其中 {plan.conflict_count} 个存在冲突"
        self.status_label.configure(text=status)

    def _on_plan_failed(self, message: str) -> None:
        self.preview.show_error(message)
        self.preview.set_can_generate(True)
        self.status_label.configure(text="生成方案失败")

    def on_cancel_plan(self) -> None:
        """取消预览：只是把界面清空，磁盘上的文件一个都没动过。"""
        self.plan = None
        self.preview.show_idle("已取消预览，文件没有任何改动。")
        self.preview.set_can_generate(bool(self.result and self.result.files))
        self.status_label.configure(text="预览已取消")

    # ------------------------------------------------------------------ 整理
    def on_confirm_plan(self) -> None:
        """用户点「确认整理」：先弹确认框，同意了才动文件。"""
        if self.is_moving or self.plan is None or not self.plan.items:
            return

        plan = self.plan
        movable = plan.total - plan.conflict_count

        lines = [f"即将把 {movable} 个文件移动到分类文件夹。"]
        if plan.conflict_count:
            lines.append(f"另有 {plan.conflict_count} 个文件存在冲突，执行时会自动跳过。")
        lines.append("")
        lines.append("· 不会删除任何文件")
        lines.append("· 不会覆盖已存在的文件")
        lines.append("· 操作记录会保存在本机")

        if movable <= 0:
            messagebox.showinfo("没有可整理的文件", "方案里的文件都存在冲突，暂时没有可移动的文件。", parent=self)
            return

        if not messagebox.askyesno("确认整理", "\n".join(lines) + "\n\n确定开始整理吗？", parent=self):
            self.status_label.configure(text="已取消整理，文件没有变化")
            return

        self.start_move(plan)

    def start_move(self, plan) -> None:
        self.is_moving = True
        self._move_cancel.clear()
        self._move_done = 0
        self._move_total = plan.total

        self.preview.show_moving(plan.total)
        self.choose_btn.configure(state="disabled")
        self.rescan_btn.configure(state="disabled")
        self.status_label.configure(text=f"正在整理 0/{plan.total}…")

        thread = threading.Thread(
            target=self._move_worker, args=(plan, RecordStore()), daemon=True
        )
        thread.start()
        self._poll_move_progress()

    def _move_worker(self, plan, store) -> None:
        """后台线程：执行移动。任何异常都收成报告，不让程序崩。"""
        try:
            report = execute_plan(
                plan,
                on_progress=self._on_move_progress,
                should_cancel=self._move_cancel.is_set,
                store=store,
            )
        except Exception as exc:                  # noqa: BLE001
            log_exception(self.logger, exc)
            message = friendly_message(exc)
            self.after(0, lambda: self._on_move_crashed(message))
        else:
            self.after(0, lambda: self._on_move_done(report))

    def _on_move_progress(self, done: int, total: int) -> None:
        """后台线程回调：只记数字，界面在主线程刷新。"""
        self._move_done, self._move_total = done, total

    def _poll_move_progress(self) -> None:
        if not self.is_moving:
            return
        if self._move_total:
            self.preview.update_move_progress(self._move_done, self._move_total)
            self.status_label.configure(
                text=f"正在整理 {self._move_done}/{self._move_total}…"
            )
        self.after(200, self._poll_move_progress)

    def on_stop_move(self) -> None:
        if self.is_moving:
            self._move_cancel.set()
            self.status_label.configure(text="正在停止…（当前文件处理完就停）")

    def _on_move_done(self, report: MoveReport) -> None:
        self.is_moving = False
        self.preview.show_move_done(report)

        self.choose_btn.configure(state="normal")
        self.rescan_btn.configure(state="normal")

        self.summary_label.configure(
            text=f"{'已停止' if report.stopped else '已完成'}整理：{report.summary_text()}。"
                 "点「重新扫描」刷新统计。"
        )
        # 旧的统计数字已经不准了，先清空，避免误导
        for tile in self.tiles.values():
            tile.update_value(0, "")

        head = "整理已停止" if report.stopped else "整理完成"
        self.status_label.configure(text=f"{head} · {report.summary_text()}")

        # 结果详情：失败的最多列 3 条，方便用户自己处理
        detail = [f"成功 {report.moved} 个", f"跳过 {report.skipped} 个", f"失败 {report.failed} 个"]
        if report.unverified:
            detail.append(f"其中 {report.unverified} 个需人工确认")
        if report.stopped:
            detail.append(f"因停止未处理 {report.remaining} 个")
        text = "，".join(detail) + "。"

        failures = [r for r in report.results if r.status == "failed"][:3]
        if failures:
            text += "\n\n失败的文件："
            for r in failures:
                text += f"\n· {r.name}：{r.message}"

        # 已经搬走但校验没过的文件：必须单独说清楚，用户得自己去目标文件夹看一眼
        unverified = [r for r in report.results if r.ok and not r.verified][:3]
        if unverified:
            text += "\n\n需要你确认的文件："
            for r in unverified:
                text += f"\n· {r.name}：{r.message}"

        text += f"\n\n本次任务编号：{report.batch_id}"
        if report.stopped:
            text += "\n\n已经移动的文件不会自动还原，需要时可以按上面的编号查记录。"

        messagebox.showinfo(head, text, parent=self)

        if report.record_errors:
            # 只报"有几条"用户没法处理，必须说清楚是哪些文件
            names = [os.path.basename(p) or p for p in report.record_failed_items[:5]]
            listing = "、".join(names)
            if report.record_errors > len(names):
                listing += f" 等 {report.record_errors} 个"

            messagebox.showwarning(
                "操作记录未完整保存",
                f"有 {report.record_errors} 个文件的操作记录没能写入文件：\n{listing}\n\n"
                "文件移动本身已经完成，但这些移动在记录里可能显示为「准备移动」。\n"
                "撤销功能会按磁盘实际情况核对，但建议先把上面这些文件记下来。\n\n"
                "常见原因：磁盘空间不足、记录文件被其他程序占用。",
                parent=self,
            )

        # 方案和扫描结果都过期了：清掉，强制重新扫描后再生成
        self.plan = None
        self.result = None
        self.preview.set_can_confirm(False)
        self.preview.set_can_generate(False)

    def _on_move_crashed(self, message: str) -> None:
        self.is_moving = False
        self.choose_btn.configure(state="normal")
        self.rescan_btn.configure(state="normal")
        # 注意：不能显示"成功 0 个"——中断前很可能已经搬走了一部分文件
        self.preview.show_move_aborted(
            "整理中断：已有部分文件可能被移动，请点「重新扫描」查看当前状态。"
        )
        self.status_label.configure(text="整理中断")
        messagebox.showerror("整理中断", message, parent=self)

        # 扫描结果已经不可信，强制重新扫描
        self.plan = None
        self.result = None

    # ------------------------------------------------------------------ 历史记录
    def on_open_history(self) -> None:
        """打开历史记录窗口（查看以前的整理，并可以把某一次撤销掉）。"""
        if self.is_moving:
            messagebox.showinfo(
                "正在整理",
                "整理正在进行，等它结束（或点「停止」）之后再查看历史记录。",
                parent=self,
            )
            return

        try:
            existing = self._history_win
            if existing is not None and existing.winfo_exists():
                # 已经开着就把它拿到前面来：两个撤销窗口同时操作同一批文件太危险
                existing.lift()
                existing.focus_force()
                return

            self._history_win = HistoryWindow(self, on_changed=self._on_history_changed)
        except Exception as exc:                  # noqa: BLE001 —— 打不开窗口也不能带崩主程序
            self._history_win = None
            log_exception(self.logger, exc)
            messagebox.showerror("打不开历史记录", friendly_message(exc), parent=self)
            self.status_label.configure(text="打开历史记录失败")

    def _on_history_changed(self, restored: int) -> None:
        """撤销完成后回调：磁盘上的文件变了，旧统计和旧方案都作废。"""
        self.plan = None
        self.result = None

        self.preview.set_can_confirm(False)
        self.preview.set_can_generate(False)
        self.preview.show_idle("文件已经恢复到原来的位置，点「重新扫描」查看最新结果。")

        # 旧的分类数字已经不准了，清空避免误导
        for tile in self.tiles.values():
            tile.update_value(0, "")

        self.summary_label.configure(text=f"已从历史记录恢复 {restored} 个文件")
        self.status_label.configure(text=f"已恢复 {restored} 个文件 · 建议重新扫描")

    # ------------------------------------------------------------------ 杂项
    def _copy_path(self, _event=None) -> None:
        """右键输入框：把完整路径复制到剪贴板。"""
        if not self.folder:
            return
        self.clipboard_clear()
        self.clipboard_append(self.folder)
        self.status_label.configure(text="已复制完整路径")
