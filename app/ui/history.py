# -*- coding: utf-8 -*-
"""历史整理记录窗口：看看以前整理过什么，并把某一次整理撤销掉。

界面结构（保持和主窗口一样的浅色、圆角、中文文案）：
    ┌──────────────────────────────────────────────┐
    │ 左侧：整理批次列表   │ 右侧：这一次移动了哪些文件 │
    └──────────────────────────────────────────────┘
    底部：状态提示 + 撤销本次整理 / 关闭

安全边界：
    本文件只负责"显示"和"转发用户操作"。
    能不能撤销、能不能搬回去，全部由 app/core/undoer.py 判断，
    界面绝不会自己决定动哪个文件。
"""

import os
import threading
from tkinter import messagebox
from typing import Callable, Dict, List, Optional

import customtkinter as ctk

from app.config import (
    COLOR_BG,
    COLOR_BORDER,
    COLOR_CARD,
    COLOR_DISABLED,
    COLOR_PRIMARY,
    COLOR_PRIMARY_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_WEAK,
)
from app.core.records import RecordError, RecordStore
from app.core.undoer import (
    REASON_ALREADY_UNDONE,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_RESTORED,
    STATUS_SKIPPED,
    BatchSummary,
    UndoPlan,
    UndoReport,
    build_undo_plan,
    execute_undo,
    list_batches,
)
from app.ui.theme import font
from app.utils.logger import get_logger, log_exception
from app.utils.paths import shorten_path

# 一次最多画多少行文件：太多会明显变卡，剩下的用一行文字说明
MAX_ROWS = 300
# 每批往界面里加多少行，加完让界面喘口气
CHUNK = 25

# 撤销条目的状态 → (显示文字, 文字颜色, 底色)
_STATUS_STYLE = {
    STATUS_READY: ("可恢复", COLOR_PRIMARY, "#EAF1FF"),
    STATUS_RESTORED: ("已恢复", "#067647", "#E7F6EE"),
    STATUS_SKIPPED: ("不会撤销", "#B54708", "#FEF0E6"),
    STATUS_FAILED: ("撤销失败", "#B42318", "#FEE4E2"),
}


class _FileRow(ctk.CTkFrame):
    """右侧列表里的一条：文件名 + 状态标签 + 「当前路径 → 原始路径」。"""

    def __init__(self, master, item, root: str, index: int):
        super().__init__(
            master,
            fg_color="#FFFFFF" if index % 2 == 0 else "#F7F9FC",
            corner_radius=6,
        )

        text, color, bg = _STATUS_STYLE.get(
            item.status, (item.status, COLOR_TEXT_WEAK, "#EEF2F7")
        )

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(5, 0))

        ctk.CTkLabel(
            top, text=item.name, font=font(12), text_color=COLOR_TEXT, anchor="w"
        ).pack(side="left")

        ctk.CTkLabel(
            top,
            text=f" {text} ",
            font=font(9),
            height=18,
            corner_radius=6,
            fg_color=bg,
            text_color=color,
        ).pack(side="right")

        # 用相对路径：根目录就写在上面，重复一长串绝对路径反而看不清
        rel_dst = os.path.relpath(item.dst, root) if root else item.dst
        rel_src = os.path.relpath(item.src, root) if root else item.src
        ctk.CTkLabel(
            self,
            text=f"{rel_dst}  →  {rel_src}",
            font=font(10),
            text_color=COLOR_TEXT_WEAK,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 4))

        if item.message and item.status != STATUS_READY:
            ctk.CTkLabel(
                self,
                text=f"· {item.message}",
                font=font(9),
                text_color=color,
                anchor="w",
            ).pack(fill="x", padx=10, pady=(0, 5))


class HistoryWindow(ctk.CTkToplevel):
    """历史整理记录 + 撤销本次整理。"""

    WIDTH = 880
    HEIGHT = 600
    LIST_WIDTH = 244

    def __init__(
        self,
        master,
        store: Optional[RecordStore] = None,
        on_changed: Optional[Callable[[int], None]] = None,
    ):
        super().__init__(master)

        self.title("历史整理记录")
        self.configure(fg_color=COLOR_BG)
        self.logger = get_logger()
        self.store = store if store is not None else RecordStore()

        # 撤销完成后通知主窗口：扫描结果已经过期了，要刷新统计
        self.on_changed = on_changed

        self.batches: List[BatchSummary] = []
        self.current: Optional[BatchSummary] = None
        self.current_plan: Optional[UndoPlan] = None
        self._batch_buttons: Dict[str, ctk.CTkButton] = {}
        self._pending_rows: List = []
        self._rows_drawn = 0
        # 分批渲染的"批次令牌"：切换批次时旧的分批回调必须立刻作废，
        # 否则会把新批次的行重复画一遍
        self._render_token = 0

        # 撤销状态（后台线程只写这些数字，界面在主线程刷新）
        self.is_undoing = False
        self._undo_cancel = threading.Event()
        self._undo_done = 0
        self._undo_total = 0
        # 正在执行的那份方案：撤销期间用户可能点别的批次，
        # 结果汇总必须以"真正执行的那份"为准，不能读 self.current_plan
        self._undo_plan: Optional[UndoPlan] = None
        self._closing = False

        self._build_ui()
        self._center_on_parent(self.WIDTH, self.HEIGHT)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.reload_batches()

        # 出现在主窗口上方，而不是被主窗口盖住
        self.after(30, self._raise_window)

    # ------------------------------------------------------------------ 窗口
    def _raise_window(self) -> None:
        if self._closing:
            return
        try:
            self.lift()
            self.focus_force()
        except Exception:                # noqa: BLE001 —— 抢焦点失败不影响使用
            pass

    def _center_on_parent(self, width: int, height: int) -> None:
        """相对主窗口居中，并保证不超出屏幕。"""
        try:
            px = self.master.winfo_rootx()
            py = self.master.winfo_rooty()
            pw = self.master.winfo_width()
            ph = self.master.winfo_height()
        except Exception:                # noqa: BLE001 —— 拿不到主窗口位置就从左上角放
            px, py, pw, ph = 80, 60, 0, 0

        x = max(0, px + (pw - width) // 2)
        y = max(0, py + max(0, (ph - height) // 3))

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = min(x, max(0, screen_w - width - 20))
        y = min(y, max(0, screen_h - height - 40))

        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(720, 480)

    # ------------------------------------------------------------------ 界面
    def _build_ui(self) -> None:
        wrap = ctk.CTkFrame(self, fg_color="transparent")
        wrap.pack(fill="both", expand=True, padx=18, pady=14)

        ctk.CTkLabel(
            wrap, text="历史整理记录", font=font(18, bold=True),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(fill="x")

        ctk.CTkLabel(
            wrap,
            text="选择一次整理，查看它移动了哪些文件，并把它们放回原处。",
            font=font(11),
            text_color=COLOR_TEXT_WEAK,
            anchor="w",
        ).pack(fill="x", pady=(2, 10))

        # 页脚先 pack（side="bottom"），窗口变矮时被压缩的是中间的列表
        self._build_footer(wrap)

        body = ctk.CTkFrame(wrap, fg_color="transparent")
        body.pack(fill="both", expand=True, pady=(0, 10))

        self._build_batch_panel(body)
        self._build_detail_panel(body)

    def _build_batch_panel(self, parent) -> None:
        panel = ctk.CTkFrame(
            parent,
            width=self.LIST_WIDTH,
            corner_radius=12,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        panel.pack(side="left", fill="y")
        panel.pack_propagate(False)

        ctk.CTkLabel(
            panel, text="整理批次", font=font(12, bold=True),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(fill="x", padx=12, pady=(12, 6))

        self.batch_box = ctk.CTkScrollableFrame(
            panel, fg_color="transparent", corner_radius=0
        )
        self.batch_box.pack(fill="both", expand=True, padx=8, pady=(0, 10))

    def _build_detail_panel(self, parent) -> None:
        panel = ctk.CTkFrame(
            parent,
            corner_radius=12,
            fg_color=COLOR_CARD,
            border_width=1,
            border_color=COLOR_BORDER,
        )
        panel.pack(side="left", fill="both", expand=True, padx=(12, 0))

        inner = ctk.CTkFrame(panel, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=12, pady=12)

        self.detail_title = ctk.CTkLabel(
            inner, text="这次整理移动的文件", font=font(12, bold=True),
            text_color=COLOR_TEXT, anchor="w",
        )
        self.detail_title.pack(fill="x")

        self.root_label = ctk.CTkLabel(
            inner, text="", font=font(10), text_color=COLOR_TEXT_WEAK, anchor="w",
        )
        self.root_label.pack(fill="x", pady=(3, 8))

        # 底部那行"另有 N 个未移动"先 pack 到底部，列表再占满剩下的空间
        self.footnote_label = ctk.CTkLabel(
            inner, text="", font=font(10), text_color=COLOR_TEXT_WEAK,
            anchor="w", justify="left",
        )
        self.footnote_label.pack(side="bottom", fill="x", pady=(6, 0))

        self.file_box = ctk.CTkScrollableFrame(
            inner,
            fg_color="#FAFBFD",
            corner_radius=10,
            border_width=1,
            border_color="#EDF1F6",
        )
        self.file_box.pack(fill="both", expand=True)

        self.placeholder = ctk.CTkLabel(
            self.file_box,
            text="左边选一次整理，这里会列出它移动成功的文件。",
            font=font(11),
            text_color="#98A2B3",
            justify="left",
        )
        self.placeholder.pack(pady=26, padx=10)

    def _build_footer(self, parent) -> None:
        footer = ctk.CTkFrame(parent, fg_color="transparent")
        footer.pack(side="bottom", fill="x")

        self.status_label = ctk.CTkLabel(
            footer, text="", font=font(10), text_color=COLOR_TEXT_WEAK, anchor="w"
        )
        self.status_label.pack(side="left")

        self.close_btn = ctk.CTkButton(
            footer,
            text="关闭",
            width=84,
            height=34,
            font=font(11),
            fg_color=COLOR_CARD,
            hover_color="#F1F4F8",
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            corner_radius=9,
            command=self._on_close,
        )
        self.close_btn.pack(side="right")

        self.undo_btn = ctk.CTkButton(
            footer,
            text="撤销本次整理",
            width=130,
            height=34,
            font=font(11, bold=True),
            fg_color=COLOR_DISABLED,
            hover_color=COLOR_PRIMARY_HOVER,
            corner_radius=9,
            state="disabled",
            command=self.on_undo_click,
        )
        self.undo_btn.pack(side="right", padx=(0, 8))

        # 撤销过程中才出现：停止
        self.stop_btn = ctk.CTkButton(
            footer,
            text="停止",
            width=84,
            height=34,
            font=font(11),
            fg_color="#B42318",
            hover_color="#8F1D14",
            corner_radius=9,
            command=self.on_stop_undo,
        )

        # 撤销进度条：默认不显示，避免一直占位置
        self.progress = ctk.CTkProgressBar(
            footer, width=150, height=6, corner_radius=3, progress_color=COLOR_PRIMARY
        )
        self.progress.set(0)

    # ------------------------------------------------------------------ 批次列表
    def reload_batches(self, select: Optional[str] = None) -> None:
        """重新读取记录并刷新左侧列表。"""
        try:
            self.batches = list_batches(self.store)
        except RecordError as exc:
            self.batches = []
            self._show_message(f"读取操作记录失败：{exc}")
            return

        for child in list(self.batch_box.winfo_children()):
            child.destroy()
        self._batch_buttons.clear()

        if not self.batches:
            self.empty_batches_label = ctk.CTkLabel(
                self.batch_box,
                text="还没有整理记录\n\n整理过一次文件后，这里会按时间列出每次整理。",
                font=font(11),
                text_color="#98A2B3",
                justify="left",
            )
            self.empty_batches_label.pack(pady=16, padx=6, anchor="w")
            self._clear_detail("还没有整理记录，先回主窗口整理一次吧。")
            return

        for summary in self.batches:
            button = ctk.CTkButton(
                self.batch_box,
                text=f"  {summary.label}",
                font=font(11),
                height=34,
                corner_radius=8,
                anchor="w",
                fg_color="transparent",
                hover_color="#EEF2F7",
                text_color=COLOR_TEXT if summary.undoable else COLOR_TEXT_WEAK,
                command=lambda b=summary.batch_id: self.select_batch(b),
            )
            button.pack(fill="x", pady=2)
            self._batch_buttons[summary.batch_id] = button

        target = select or (self.current.batch_id if self.current else "") or self.batches[0].batch_id
        known = {b.batch_id for b in self.batches}
        self.select_batch(target if target in known else self.batches[0].batch_id)

    def _highlight(self, batch_id: str) -> None:
        for bid, button in self._batch_buttons.items():
            if bid == batch_id:
                button.configure(fg_color="#EAF1FF", text_color=COLOR_PRIMARY)
            else:
                summary = next((b for b in self.batches if b.batch_id == bid), None)
                button.configure(
                    fg_color="transparent",
                    text_color=COLOR_TEXT if summary and summary.undoable else COLOR_TEXT_WEAK,
                )

    def select_batch(self, batch_id: str) -> None:
        """选中一个批次：重新生成撤销方案并显示详情。"""
        summary = next((b for b in self.batches if b.batch_id == batch_id), None)
        if summary is None:
            return

        self.current = summary
        self._highlight(batch_id)

        try:
            plan = build_undo_plan(self.store, batch_id)
        except RecordError as exc:
            self.current_plan = None
            self._clear_detail(f"读取操作记录失败：{exc}")
            self._set_undo_enabled(False)
            return
        except Exception as exc:              # noqa: BLE001 —— 单批次出错不能带崩窗口
            log_exception(self.logger, exc)
            self.current_plan = None
            self._clear_detail(f"这个批次的记录读不出来：{exc}")
            self._set_undo_enabled(False)
            return

        self.current_plan = plan
        self._render_detail(summary, plan)

    # ------------------------------------------------------------------ 详情
    def _render_detail(self, summary: BatchSummary, plan: UndoPlan) -> None:
        self.detail_title.configure(text=f"{summary.time_text} 的整理")

        if plan.root:
            self.root_label.configure(
                text=f"整理目录：{shorten_path(plan.root, 76)}（右键可复制完整路径）"
            )
            self.root_label.bind("<Button-3>", lambda _e: self._copy_root(plan.root))
        else:
            self.root_label.configure(text="整理目录：记录里没有可用的目录信息")
            self.root_label.unbind("<Button-3>")

        self._clear_rows()

        if not plan.items:
            self._show_placeholder(
                "这次整理没有移动任何文件（全部跳过或失败），没有可撤销的内容。"
            )
        else:
            self._pending_rows = plan.items[:MAX_ROWS]
            self._rows_drawn = 0
            self._render_chunk(self._render_token)

        self._set_undo_enabled(plan.ready_count > 0)
        undone = sum(1 for i in plan.items if i.message == REASON_ALREADY_UNDONE)
        if plan.items and plan.ready_count == 0 and undone == len(plan.items):
            self._update_status("这个批次的文件已经全部撤销过了，文件都在原位置")
        elif plan.items:
            self._update_status(
                f"{plan.summary_text()}（这次整理共移动成功 {summary.moved} 个）"
            )
        else:
            self._update_status("这次整理没有可撤销的文件")

        # 底部：整理时就没搬走的文件，说明清楚它们不在撤销范围内
        notes = []
        if summary.untouched:
            notes.append(f"另有 {summary.untouched} 个未移动（跳过/失败），不会撤销")
        if summary.uncertain:
            notes.append(f"{summary.uncertain} 个状态不确定（整理中断），不会撤销")
        self.footnote_label.configure(text="；".join(notes) if notes else "")

    def _render_chunk(self, token: int) -> None:
        """分批往列表里加行，避免一次加几百行把界面卡住。"""
        if self._closing or token != self._render_token:
            return

        root = self.current_plan.root if self.current_plan else ""
        start = self._rows_drawn
        end = min(start + CHUNK, len(self._pending_rows))

        for index in range(start, end):
            _FileRow(self.file_box, self._pending_rows[index], root, index).pack(
                fill="x", pady=2, padx=4
            )

        self._rows_drawn = end

        if end < len(self._pending_rows):
            self.after(1, lambda: self._render_chunk(token))
            return

        total = self.current_plan.total if self.current_plan else 0
        if len(self._pending_rows) < total:
            ctk.CTkLabel(
                self.file_box,
                text=f"文件较多，仅显示前 {len(self._pending_rows)} 条（共 {total} 条）。",
                font=font(10),
                text_color=COLOR_TEXT_WEAK,
            ).pack(pady=8)

        self._scroll_to_top()

    def _clear_rows(self) -> None:
        self._pending_rows = []
        self._rows_drawn = 0
        self._render_token += 1            # 让还在排队的分批渲染立刻作废
        self.placeholder = None            # 旧的提示已被销毁，必须置空
        for child in list(self.file_box.winfo_children()):
            child.destroy()

    def _show_placeholder(self, text: str) -> None:
        self.placeholder = ctk.CTkLabel(
            self.file_box, text=text, font=font(11), text_color="#98A2B3",
            justify="left", wraplength=480,
        )
        self.placeholder.pack(pady=26, padx=10, anchor="w")

    def _clear_detail(self, message: str) -> None:
        self.current_plan = None           # 详情被清空后，旧方案不能再被撤销
        self.detail_title.configure(text="这次整理移动的文件")
        self.root_label.configure(text="")
        self.footnote_label.configure(text="")
        self._clear_rows()
        self._show_placeholder(message)
        self._set_undo_enabled(False)

    def _scroll_to_top(self) -> None:
        """分批加行后画布会停在中间，手动滚回顶部，否则看起来像漏了前几条。"""
        try:
            self.file_box._parent_canvas.yview_moveto(0)   # CTk 内部画布
        except Exception:                                  # noqa: BLE001 —— 版本差异时忽略
            pass

    def _copy_root(self, root: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(root)
            self._update_status("已复制整理目录的完整路径")
        except Exception:                # noqa: BLE001 —— 剪贴板被占用时不打扰用户
            pass

    # ------------------------------------------------------------------ 状态
    def _set_undo_enabled(self, enabled: bool) -> None:
        if enabled and not self.is_undoing:
            self.undo_btn.configure(state="normal", fg_color=COLOR_PRIMARY)
        else:
            self.undo_btn.configure(state="disabled", fg_color=COLOR_DISABLED)

    def _update_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def _show_message(self, message: str) -> None:
        self._clear_detail(message)
        self._update_status(message)

    # ------------------------------------------------------------------ 撤销
    def on_undo_click(self) -> None:
        plan = self.current_plan
        summary = self.current

        if self.is_undoing or plan is None or summary is None:
            return

        if plan.ready_count <= 0:
            messagebox.showinfo(
                "没有可撤销的文件",
                "这个批次里没有可以安全恢复的文件。\n"
                "可能原因：文件已经被移走、原位置已有同名文件，或记录状态不确定。",
                parent=self,
            )
            return

        lines = [
            f"即将把 {plan.ready_count} 个文件恢复到整理前的位置。",
            "",
            "本次撤销保证：",
            "· 不会删除任何文件",
            "· 不会覆盖已存在的文件（原位置有同名文件时会跳过）",
            "· 每个文件都会重新核对路径，记录对不上就跳过",
        ]
        if plan.skipped_count:
            lines.append("")
            lines.append(f"另有 {plan.skipped_count} 个文件不会撤销（详情见列表里的原因）。")

        if not messagebox.askyesno(
            "确认撤销", "\n".join(lines) + "\n\n确定开始撤销吗？", parent=self
        ):
            self._update_status("已取消撤销，文件没有变化")
            return

        self.start_undo(plan)

    def start_undo(self, plan: UndoPlan) -> None:
        self.is_undoing = True
        self._undo_plan = plan
        self._undo_cancel.clear()
        self._undo_done = 0
        self._undo_total = plan.ready_count

        self._enter_undo_mode()
        self.progress.set(0)
        self._update_status(f"正在恢复 0/{self._undo_total}…")

        # 后台线程用自己的 RecordStore：记录文件每次写入都会重新打开，线程安全
        worker_store = RecordStore(getattr(self.store, "path", None))
        thread = threading.Thread(
            target=self._undo_worker, args=(plan, worker_store), daemon=True
        )
        thread.start()
        self._poll_undo_progress()

    def _undo_worker(self, plan: UndoPlan, store: RecordStore) -> None:
        """后台线程：只做撤销，绝对不碰界面控件。"""
        try:
            report = execute_undo(
                plan,
                store=store,
                on_progress=self._on_undo_progress,
                should_cancel=self._undo_cancel.is_set,
            )
        except Exception as exc:                  # noqa: BLE001 —— 任何异常都要变成中文提示
            log_exception(self.logger, exc)
            message = str(exc)
            if not self._closing:
                self.after(0, lambda: self._on_undo_crashed(message))
        else:
            if not self._closing:
                self.after(0, lambda: self._on_undo_done(report))

    def _on_undo_progress(self, done: int, total: int) -> None:
        """后台线程回调：只记数字，界面在主线程刷新。"""
        self._undo_done, self._undo_total = done, total

    def _poll_undo_progress(self) -> None:
        if self._closing or not self.is_undoing:
            return
        if self._undo_total:
            self.progress.set(min(1.0, self._undo_done / self._undo_total))
            self._update_status(f"正在恢复 {self._undo_done}/{self._undo_total}…")
        self.after(200, self._poll_undo_progress)

    def on_stop_undo(self) -> None:
        if self.is_undoing:
            self._undo_cancel.set()
            self._update_status("正在停止…（当前文件处理完就停）")

    def _enter_undo_mode(self) -> None:
        """撤销中：把「撤销 / 关闭」收起来，只留「停止」+ 进度条。"""
        self.undo_btn.pack_forget()
        self.close_btn.pack_forget()
        # 先 pack 的在最右边：「停止」钉在最右，进度条在它左边
        self.stop_btn.pack(side="right")
        self.progress.pack(side="right", padx=(0, 10))
        self.close_btn.configure(state="disabled")

    def _exit_undo_mode(self) -> None:
        self.stop_btn.pack_forget()
        self.progress.pack_forget()
        self.progress.set(0)
        self.close_btn.configure(state="normal")
        self.close_btn.pack(side="right")
        self.undo_btn.pack(side="right", padx=(0, 8))

    def _on_undo_done(self, report: UndoReport) -> None:
        self.is_undoing = False
        self._exit_undo_mode()

        plan = self._undo_plan
        self._undo_plan = None
        # 方案阶段就判定"不动"的条目不会进入 results（根本没被碰过），
        # 但用户看到的结果里必须把它们算上，否则会以为"跳过 0 个"。
        pre_skipped = [i for i in plan.items if i.status == STATUS_SKIPPED] if plan else []
        total_skipped = report.skipped + len(pre_skipped)

        head = "撤销已停止" if report.stopped else "撤销完成"
        self._update_status(
            f"{head} · 成功恢复 {report.restored} 个，跳过 {total_skipped} 个，"
            f"失败 {report.failed} 个"
        )

        lines = [
            f"成功恢复 {report.restored} 个，跳过 {total_skipped} 个，失败 {report.failed} 个。"
        ]
        if pre_skipped:
            lines.append(f"（其中 {len(pre_skipped)} 个在撤销前检查时就已确定不动，没有碰过文件）")
        if report.unverified:
            lines.append(f"其中 {report.unverified} 个文件大小与移动前不一致，请手动确认。")
        if report.stopped:
            lines.append(f"因停止未处理 {report.remaining} 个，它们还在原来的分类文件夹里。")

        skips = [(r.name, r.message) for r in report.results if r.status == STATUS_SKIPPED]
        if len(skips) < 3:
            skips += [(i.name, i.message) for i in pre_skipped][: 3 - len(skips)]
        if skips:
            lines.append("")
            lines.append("跳过的原因：")
            for name, message in skips:
                lines.append(f"· {name}：{message}")

        fails = [r for r in report.results if r.status == STATUS_FAILED][:3]
        if fails:
            lines.append("")
            lines.append("失败的文件（仍在分类文件夹里）：")
            for r in fails:
                lines.append(f"· {r.name}：{r.message}")

        lines.append("")
        lines.append(f"撤销编号：{report.undo_id}")

        messagebox.showinfo(head, "\n".join(lines), parent=self)

        if report.record_errors:
            # 只报"有几条"用户没法处理，必须说清楚是哪些文件
            names = [os.path.basename(p) or p for p in report.record_failed_items[:5]]
            listing = "、".join(names)
            if report.record_errors > len(names):
                listing += f" 等 {report.record_errors} 个"

            messagebox.showwarning(
                "撤销记录未完整保存",
                f"有 {report.record_errors} 个文件的撤销记录没能写入文件：\n{listing}\n\n"
                "文件恢复本身已经完成，但这些还原在记录里没有留下痕迹，"
                "下次可能会重复处理同一个文件。\n\n"
                "常见原因：磁盘空间不足、记录文件被其他程序占用。",
                parent=self,
            )

        # 批次列表要刷新：这个批次的可撤销数应该变成 0
        self.reload_batches(select=report.batch_id)

        if report.restored and self.on_changed is not None:
            try:
                self.on_changed(report.restored)
            except Exception:            # noqa: BLE001 —— 主窗口刷新失败不影响撤销结果
                pass

    def _on_undo_crashed(self, message: str) -> None:
        self.is_undoing = False
        self._undo_plan = None
        self._exit_undo_mode()
        # 不能显示"成功 0 个"：中断前很可能已经恢复了一部分文件
        self._set_undo_enabled(self.current_plan is not None and self.current_plan.ready_count > 0)
        self._update_status("撤销中断，请刷新后重新查看")
        messagebox.showerror(
            "撤销中断",
            "撤销过程中出现问题，可能有部分文件已经恢复。\n\n"
            f"详细信息：{message}\n\n"
            "建议关掉这个窗口重新打开，按列表里的实际状态判断。",
            parent=self,
        )
        self.reload_batches(select=self.current.batch_id if self.current else None)

    # ------------------------------------------------------------------ 关闭
    def _on_close(self) -> None:
        """撤销进行中不允许关窗：否则后台线程还会往回调用界面，容易出问题。"""
        if self.is_undoing:
            messagebox.showinfo(
                "正在撤销",
                "撤销正在进行，请等它完成，或点「停止」后再关闭窗口。",
                parent=self,
            )
            return

        self._closing = True
        self.destroy()
