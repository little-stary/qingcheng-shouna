# -*- coding: utf-8 -*-
"""整理预览面板（第 3 步）。

职责很单一：把「整理方案」画出来，不负责生成方案、更不负责移动文件。
方案由 app/core/planner.py 算好，这里只管显示。

为什么单独成文件：
    主窗口已经有"选文件夹 + 扫描 + 统计"三块，再塞一个几百行的表格会很难读。
    拆出来以后，主窗口只剩布局和事件转发。
"""

import os

import os

import customtkinter as ctk

from app.config import (
    COLOR_BORDER,
    COLOR_DISABLED,
    COLOR_DISABLED,
    COLOR_CARD,
    COLOR_PRIMARY,
    COLOR_PRIMARY_HOVER,
    COLOR_TEXT,
    COLOR_TEXT_WEAK,
)
from app.core.planner import Plan, PlanItem
from app.ui.theme import font
from app.ui.widgets import Card
from app.utils.paths import shorten_path

# 一次最多画多少行：太多会明显变卡，剩下的用文字说明代替
MAX_ROWS = 200
# 每次往界面里加多少行，加完让界面刷新一下，避免"点一下就卡住两秒"
CHUNK = 25


class PlanRow(ctk.CTkFrame):
    """一条预览：第一行是文件名 + 类型 + 冲突标记，第二行是"从哪来到哪去"。"""

    def __init__(self, master, item: PlanItem, index: int, source_dir: str = ""):
        super().__init__(
            master,
            fg_color="#FFFFFF" if index % 2 == 0 else "#F7F9FC",
            corner_radius=6,
        )

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=10, pady=(5, 0))

        ctk.CTkLabel(
            top,
            text=item.name,
            font=font(12),
            text_color="#B42318" if item.conflict else COLOR_TEXT,
            anchor="w",
        ).pack(side="left")

        if item.conflict:
            ctk.CTkLabel(
                top,
                text=f" {item.status_text} ",
                font=font(9),
                height=18,
                corner_radius=6,
                fg_color="#FEE4E2",
                text_color="#B42318",
            ).pack(side="right", padx=(6, 0))

        ctk.CTkLabel(
            top,
            text=f" {item.category} ",
            font=font(9),
            height=18,
            corner_radius=6,
            fg_color="#EEF2F7",
            text_color="#475467",
        ).pack(side="right")

        # 用相对路径显示：所选文件夹就在页面上方，重复写一长串绝对路径反而看不清
        rel_src = os.path.relpath(item.src, source_dir) if source_dir else item.src
        rel_dst = os.path.relpath(item.dst, source_dir) if source_dir else item.dst
        ctk.CTkLabel(
            self,
            text=f"{rel_src}  →  {rel_dst}",
            font=font(10),
            text_color=COLOR_TEXT_WEAK,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 5))


class PreviewPanel(Card):
    """第 3 步：生成方案 → 预览 → 取消。"""

    def __init__(self, master, on_generate, on_cancel, on_confirm, on_stop):
        super().__init__(master, "第 3 步 · 整理预览")

        self.on_generate = on_generate
        self.on_cancel = on_cancel
        self.on_confirm = on_confirm
        self.on_stop = on_stop
        self._rows = 0
        self._pending_items: list[PlanItem] = []
        self._pending_total = 0
        self._source_dir = ""

        # ---- 顶部操作条 ----
        bar = ctk.CTkFrame(self.inner, fg_color="transparent")
        bar.pack(fill="x", pady=(0, 6))

        self.summary_label = ctk.CTkLabel(
            bar,
            text="扫描完成后，点右侧按钮生成整理方案。",
            font=font(11),
            text_color=COLOR_TEXT_WEAK,
            anchor="w",
        )
        self.summary_label.pack(side="left", fill="x", expand=True)

        self.cancel_btn = ctk.CTkButton(
            bar,
            text="取消",
            width=66,
            height=30,
            font=font(11),
            fg_color=COLOR_CARD,
            hover_color="#F1F4F8",
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            corner_radius=8,
            state="disabled",
            command=self.on_cancel,
        )
        self.cancel_btn.pack(side="right", padx=(6, 0))

        # 生成方案是"次要动作"，用描边按钮；确认整理是"关键动作"，用实心蓝色
        self.generate_btn = ctk.CTkButton(
            bar,
            text="生成整理方案",
            width=112,
            height=30,
            font=font(11),
            fg_color=COLOR_CARD,
            hover_color="#F1F4F8",
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            corner_radius=8,
            state="disabled",
            command=self.on_generate,
        )
        self.generate_btn.pack(side="right", padx=(6, 0))

        self.confirm_btn = ctk.CTkButton(
            bar,
            text="确认整理",
            width=92,
            height=30,
            font=font(11, bold=True),
            fg_color=COLOR_DISABLED,
            hover_color=COLOR_PRIMARY_HOVER,
            corner_radius=8,
            state="disabled",
            command=self.on_confirm,
        )
        self.confirm_btn.pack(side="right", padx=(6, 0))

        # 整理过程中才出现：停止
        self.stop_btn = ctk.CTkButton(
            bar,
            text="停止整理",
            width=92,
            height=30,
            font=font(11),
            fg_color="#B42318",
            hover_color="#8F1D14",
            corner_radius=8,
            command=self.on_stop,
        )

        # 整理时的进度条：默认不显示，避免占位置
        self.progress = ctk.CTkProgressBar(
            self.inner, height=6, corner_radius=3, progress_color=COLOR_PRIMARY
        )
        self.progress.set(0)

        # ---- 列表容器 ----
        # CTkScrollableFrame 不认 height 参数（内部按内容算最小高度），
        # 所以套一个固定高度的容器并关掉"随内容撑开"，最小高度才可控。
        self.list_holder = ctk.CTkFrame(self.inner, fg_color="transparent", height=70)
        self.list_holder.pack(fill="both", expand=True)
        self.list_holder.pack_propagate(False)

        self.list_box = ctk.CTkScrollableFrame(
            self.list_holder,
            fg_color="#FAFBFD",
            corner_radius=10,
            border_width=1,
            border_color="#EDF1F6",
        )
        self.list_box.pack(fill="both", expand=True)

        self.placeholder = ctk.CTkLabel(
            self.list_box,
            text="这里会列出「哪个文件 → 去哪个文件夹」\n确认之前不会有任何文件被移动。",
            font=font(11),
            text_color="#98A2B3",
            justify="center",
        )
        self.placeholder.pack(pady=26)

    # ------------------------------------------------------------------ 状态
    def set_can_generate(self, enabled: bool) -> None:
        self.generate_btn.configure(state="normal" if enabled else "disabled")

    def show_idle(self, text: str = "扫描完成后，点右侧按钮生成整理方案。") -> None:
        self._clear()
        self.summary_label.configure(text=text)
        self.cancel_btn.configure(state="disabled")
        self._show_placeholder(
            "这里会列出「哪个文件 → 去哪个文件夹」\n确认之前不会有任何文件被移动。"
        )

    def show_loading(self) -> None:
        self._clear()
        self.summary_label.configure(text="正在生成整理方案…")
        self.cancel_btn.configure(state="disabled")
        self._show_placeholder("正在计算每个文件的目标位置…")

    def show_error(self, message: str) -> None:
        self._clear()
        self.summary_label.configure(text=message)
        self.cancel_btn.configure(state="disabled")
        self._show_placeholder("没能生成方案，请重新扫描后再试。")

    def show_plan(self, plan: Plan) -> None:
        self._clear()
        self.cancel_btn.configure(state="normal")
        self.set_can_confirm(True)

        conflict = plan.conflict_count
        parts = [f"计划整理 {plan.total} 个文件"]
        if conflict:
            parts.append(f"其中 {conflict} 个存在冲突（标红）")
        if plan.new_folders:
            parts.append(f"需要新建 {len(plan.new_folders)} 个文件夹（本阶段不会创建）")
        self.summary_label.configure(text=" · ".join(parts))

        if plan.total == 0:
            self._show_placeholder("这个文件夹里没有可整理的文件。")
            return

        self._pending_total = plan.total
        self._source_dir = plan.source_dir
        self._pending_items = plan.items[:MAX_ROWS]
        self._rows = 0
        self._render_chunk()

    # ------------------------------------------------------------------ 整理中
    def set_can_confirm(self, enabled: bool) -> None:
        if enabled:
            self.confirm_btn.configure(state="normal", fg_color=COLOR_PRIMARY)
        else:
            self.confirm_btn.configure(state="disabled", fg_color=COLOR_DISABLED)

    def _enter_moving_mode(self) -> None:
        """整理中：把生成/取消/确认收起来，只留「停止整理」。"""
        for btn in (self.cancel_btn, self.generate_btn, self.confirm_btn):
            btn.pack_forget()
        self.stop_btn.pack(side="right")
        self.progress.pack(fill="x", pady=(0, 6))

    def _exit_moving_mode(self) -> None:
        self.stop_btn.pack_forget()
        self.progress.pack_forget()
        # 重新按"从右到左"的顺序摆回去，保证和原来一致
        self.cancel_btn.pack(side="right", padx=(6, 0))
        self.generate_btn.pack(side="right", padx=(6, 0))
        self.confirm_btn.pack(side="right", padx=(6, 0))

    def show_moving(self, total: int) -> None:
        self._enter_moving_mode()
        self.progress.set(0)
        self.summary_label.configure(text=f"正在整理 0/{total}…（随时可以停止）")

    def update_move_progress(self, done: int, total: int) -> None:
        if total:
            self.progress.set(min(1.0, done / total))
        self.summary_label.configure(text=f"正在整理 {done}/{total}…（随时可以停止）")

    def show_move_done(self, report) -> None:
        """整理结束：显示成功/跳过/失败的数量，并把「确认整理」关掉。"""
        self._exit_moving_mode()
        self.progress.set(0)
        prefix = "已停止 · " if report.stopped else "整理完成 · "
        self.summary_label.configure(text=prefix + report.summary_text())

        # 方案已经执行过了：把旧列表换成一句提示，避免用户以为文件还留在原地
        self._clear()
        self._show_placeholder("本次整理已结束。\n点「重新扫描」可以查看整理之后的结果。")

        self.set_can_confirm(False)
        self.generate_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

    def show_move_aborted(self, summary: str) -> None:
        """整理异常中断：这时具体数字已经不可信，只说明状态，不报"成功 0 个"。"""
        self._exit_moving_mode()
        self.progress.set(0)
        self.summary_label.configure(text=summary)

        self._clear()
        self._show_placeholder(
            "整理已中断。\n已经移动的文件不会自动还原，点「重新扫描」查看当前状态。"
        )

        self.set_can_confirm(False)
        self.generate_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")

    # ------------------------------------------------------------------ 内部
    def _clear(self) -> None:
        self._pending_items = []
        self._rows = 0
        self.placeholder = None          # 旧提示已被销毁，必须置空，否则再 configure 会报错
        for child in list(self.list_box.winfo_children()):
            child.destroy()

    def _show_placeholder(self, text: str) -> None:
        """重建一条居中的占位提示（销毁后不能复用，必须新建）。"""
        self.placeholder = ctk.CTkLabel(
            self.list_box,
            text=text,
            font=font(11),
            text_color="#98A2B3",
            justify="center",
        )
        self.placeholder.pack(pady=26)

    def _render_chunk(self) -> None:
        """分批往列表里加行：一次加 25 行，加完让界面喘口气再加下一批。"""
        start = self._rows
        end = min(start + CHUNK, len(self._pending_items))

        for index in range(start, end):
            PlanRow(self.list_box, self._pending_items[index], index, self._source_dir).pack(
                fill="x", pady=2, padx=4
            )

        self._rows = end

        if end < len(self._pending_items):
            # 还有剩下的，交给下一次事件循环，界面不会卡住
            self.after(1, self._render_chunk)
            return

        if len(self._pending_items) < self._pending_total:
            ctk.CTkLabel(
                self.list_box,
                text=f"文件较多，仅显示前 {len(self._pending_items)} 条"
                f"（共 {self._pending_total} 条，整理时会处理全部）。",
                font=font(10),
                text_color=COLOR_TEXT_WEAK,
            ).pack(pady=8)

        self._scroll_to_top()

    def _scroll_to_top(self) -> None:
        """新方案渲染完后把列表滚回顶部。

        分批添加行时，画布会停在中间位置（看起来像漏了前几条），必须手动归零。
        """
        try:
            self.list_box._parent_canvas.yview_moveto(0)   # CTk 内部画布
        except Exception:                                  # noqa: BLE001 —— 版本差异时忽略
            pass

