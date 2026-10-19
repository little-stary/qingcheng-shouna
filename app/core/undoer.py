# -*- coding: utf-8 -*-
"""撤销整理：把某一次整理移动过的文件，按记录搬回原来的位置。

设计原则（每一条都对应需求里的一条硬约束）：
    1. **不信任历史记录**：记录只能说明"当时想做什么"，能不能撤销要看磁盘现在是什么样。
       所以生成方案时查一遍、真正动手前**再查一遍**，两次检查内容完全一样。
    2. **只看状态明确的记录**：只撤销 status="moved" 的条目；
       status="moving" 表示上次整理中途断了（可能搬了也可能没搬），一律跳过并说明原因。
    3. **不越界**：整理根目录是从记录里的 dst 反推出来的（root\\分类\\文件名 → root），
       而且每一条路径都要通过 is_inside / is_direct_child 校验，
       校验基于 realpath，能识破目录联接和符号链接。
    4. **不删除、不覆盖**：全程只用 mover._move_file（内部是 os.rename），
       原始位置已经有同名东西就跳过。
    5. **不谎报**：移动失败记 failed，移动后大小不一致记 restored + verified=False，
       记录写不进文件也要把数量报给界面。
    6. **不跨批次**：一次只处理一个 batch_id，撤销本身记在新批次（undo-xxxx）里，
       并用 undo_of 指回原批次，下次列批次时这些撤销批次不会再出现。

本模块是纯逻辑，**不 import tkinter**，界面怎么显示都不影响这里的判断。
"""

import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from app.core.mover import _move_file
from app.core.records import (
    STATUS_MOVING,
    RecordError,
    RecordStore,
    now_text,
)
from app.utils.paths import is_direct_child, is_inside

# ---- 撤销条目的四种状态 ----
STATUS_READY = "ready"          # 检查通过，可以恢复
STATUS_SKIPPED = "skipped"      # 条件不满足，不动它（附原因）
STATUS_RESTORED = "restored"    # 已经放回原位置
STATUS_FAILED = "failed"        # 尝试过但失败了

# 原始整理记录里表示"确实移动成功"的状态（与 mover.STATUS_MOVED 一致）
_MOVED = "moved"
_ORIGINAL_SKIPPED = "skipped"
_ORIGINAL_FAILED = "failed"

# 撤销记录用独立的 phase 值，方便以后一眼看出这是撤销而不是整理
PHASE_UNDO = "undo"

# "这个文件已经被撤销过"的统一说法：界面也会按这句话判断能否给用户更准确的提示
REASON_ALREADY_UNDONE = "这次整理已经撤销过"


# ====================================================================== 数据结构
@dataclass
class UndoItem:
    """撤销方案/结果里的一条：一个文件要不要从 dst 搬回 src。"""

    src: str                       # 原始路径（要恢复到的位置）
    dst: str                       # 当前路径（文件现在躺的地方）
    category: str                  # 分类（只是显示用）
    status: str                    # ready / skipped / restored / failed
    message: str = ""              # 跳过或失败的原因
    verified: bool = True          # 恢复后的校验是否通过

    @property
    def name(self) -> str:
        return os.path.basename(self.dst) or self.dst

    @property
    def ok(self) -> bool:
        return self.status == STATUS_RESTORED


@dataclass
class UndoPlan:
    """一次撤销的完整方案（只是计算结果，不动任何文件）。"""

    batch_id: str
    root: str
    items: List[UndoItem] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def ready_count(self) -> int:
        return sum(1 for i in self.items if i.status == STATUS_READY)

    @property
    def skipped_count(self) -> int:
        return sum(1 for i in self.items if i.status == STATUS_SKIPPED)

    @property
    def restored_count(self) -> int:
        return sum(1 for i in self.items if i.status == STATUS_RESTORED)

    @property
    def failed_count(self) -> int:
        return sum(1 for i in self.items if i.status == STATUS_FAILED)

    def summary_text(self) -> str:
        parts = [f"可撤销 {self.ready_count} 个"]
        if self.skipped_count:
            parts.append(f"不会撤销 {self.skipped_count} 个")
        return "，".join(parts)


@dataclass
class UndoReport:
    """一次撤销的汇总结果。"""

    undo_id: str
    batch_id: str
    results: List[UndoItem] = field(default_factory=list)
    stopped: bool = False
    record_errors: int = 0                                          # 有几条撤销记录没写进文件
    record_failed_items: List[str] = field(default_factory=list)    # 上面这些文件的原始路径
    total: int = 0                                                  # 计划里准备撤销多少个

    @property
    def restored(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_RESTORED)

    @property
    def skipped(self) -> int:
        """**只统计"执行阶段"跳过的文件数**（真的走到搬运那一步、当场被拦下的）。

        契约（调用方必读）：
            `build_undo_plan` 阶段就被判为 skipped 的条目**不会进入 results**
            （它们从来没被碰过，`execute_undo` 只处理 ready 条目），
            所以"用户视角的跳过总数" = `plan.skipped_count + report.skipped`。
            界面层（app/ui/history.py）就是按这个口径把两部分加起来显示的。
            只读 `report.skipped` 会少算方案阶段的跳过量，看起来像"跳过 0 个"。
        """
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FAILED)

    @property
    def unverified(self) -> int:
        """已经放回去了、但事后校验没通过的文件数（需要用户自己确认）。"""
        return sum(1 for r in self.results if r.ok and not r.verified)

    @property
    def remaining(self) -> int:
        """因为用户停止而没来得及处理的文件数。"""
        return max(0, self.total - len(self.results))

    def summary_text(self) -> str:
        parts = [f"成功恢复 {self.restored} 个"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 个")
        if self.failed:
            parts.append(f"失败 {self.failed} 个")
        if self.unverified:
            parts.append(f"其中 {self.unverified} 个需人工确认")
        if self.stopped:
            parts.append(f"因停止未处理 {self.remaining} 个")
        return "，".join(parts)


@dataclass
class BatchSummary:
    """历史窗口左侧列表里的一行：一次整理批次的大致情况。"""

    batch_id: str
    started_at: str
    root: str
    moved: int                     # 记录里状态为 moved 的条目数
    skipped: int                   # 整理时跳过的
    failed: int                    # 整理时失败的
    uncertain: int                 # 状态不确定（上次整理中断，status="moving"）
    undoable: int                  # 记录为 moved、且文件现在确实还在目标位置的条目数

    @property
    def untouched(self) -> int:
        """整理时就没搬走的文件数（跳过 + 失败）。"""
        return self.skipped + self.failed

    @property
    def time_text(self) -> str:
        return (self.started_at or "")[:16] or "时间未知"

    @property
    def label(self) -> str:
        """左侧按钮上的文字：2026-10-18 21:30 · 可撤销 5 个。"""
        if self.undoable:
            return f"{self.time_text} · 可撤销 {self.undoable} 个"
        if self.uncertain and not self.moved:
            return f"{self.time_text} · 状态不确定"
        return f"{self.time_text} · 无可撤销文件"


# ====================================================================== 小工具
def new_undo_id() -> str:
    """撤销任务自己的编号。加前缀，方便在记录里和普通整理批次区分。"""
    return "undo-" + uuid.uuid4().hex[:12]


def _root_of(dst: str) -> str:
    """从 dst（root\\分类\\文件名）反推出整理根目录 root。

    记录里没有单独存"用户当年选的是哪个文件夹"，但目标路径的形状是固定的：
        root \\ 分类文件夹 \\ 文件名
    所以往上退两层就是 root。推不出来的（比如路径残缺）返回空字符串，
    调用方会把这种条目整条跳过——宁可撤销不了，也不能乱动别的目录。
    """
    if not dst:
        return ""
    parent = os.path.dirname(dst)
    if not parent:
        return ""
    return os.path.dirname(parent)


def _depth(path: str) -> int:
    """路径层级，用来决定撤销顺序（先放回更深的位置）。"""
    return len([p for p in os.path.normpath(path).split(os.sep) if p])


def _permission_text(exc: PermissionError) -> str:
    """把 Windows 的"拒绝访问"细分成"被占用"和"没权限"两种情况。

    这里故意不复用 mover 里的同名函数：那个版本的文案是"已跳过"，
    而撤销失败时文件其实还留在分类文件夹里、必须重试，说成"跳过"会误导用户。
    """
    code = getattr(exc, "winerror", None)
    if code in (32, 33):            # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
        return "文件正被其他程序占用，撤销失败（关闭该文件后重试）"
    if code == 5:                   # ERROR_ACCESS_DENIED
        return "没有权限把文件放回原位置，撤销失败"
    return "没有权限，或文件正被其他程序占用，撤销失败"


def _os_error_text(exc: OSError) -> str:
    """把常见的系统错误翻译成用户能看懂的话（撤销方向）。"""
    code = getattr(exc, "winerror", None)
    err = getattr(exc, "errno", None)

    if err in (28, 122) or code == 112:      # ENOSPC / ERROR_DISK_FULL
        return "磁盘空间不足，撤销失败"
    if err == 18 or code == 17:              # EXDEV / ERROR_NOT_SAME_DEVICE
        return "不支持跨磁盘恢复，撤销失败"
    if code == 206 or err == 36:             # ERROR_FILENAME_EXCED_RANGE
        return "原始路径过长，Windows 无法处理"

    text = str(exc).lower()
    if "space" in text:
        return "磁盘空间不足，撤销失败"
    if "too long" in text:
        return "原始路径过长，Windows 无法处理"

    return f"撤销失败：{exc.strerror or exc}"


# ====================================================================== 记录整理
def _split_records(records: List[Dict]) -> Tuple[Dict[str, List[Dict]], Set[Tuple[str, str]]]:
    """把"原始整理批次"和"撤销记录"分开。

    返回：
        (原始批次字典, 已撤销过的 (原批次, 原始路径) 集合)

    判断依据：撤销记录一定带 undo_of 字段。反过来，带 undo_of 的记录
    一定不是用户眼中的"一次整理"，所以不能出现在历史批次列表里。
    """
    originals: Dict[str, List[Dict]] = {}
    restored: Set[Tuple[str, str]] = set()

    for entry in records:
        undo_of = entry.get("undo_of")
        if undo_of:
            if entry.get("status") == STATUS_RESTORED:
                src = entry.get("src")
                if src:
                    restored.add((undo_of, src))
            continue

        batch_id = entry.get("batch_id")
        if not batch_id:
            continue
        originals.setdefault(batch_id, []).append(entry)

    return originals, restored


def _root_from_entries(entries: List[Dict]) -> str:
    """批次根目录：从记录里的路径反推，取出现次数最多的那个。

    为什么按"多数"而不是"第一条"：
        如果记录里混进了一两条被人改过、或者来自别的目录的条目，
        拿第一条当标准会让**所有**条目都变成"目录不一致"，用户看不出真正的问题在哪。
        按多数定 root 之后，异常的那一两条会各自报出更准确的原因
        （文件名不一致 / 不在整理目录内 / 文件已不存在 …）。
    """
    for status in (_MOVED, _ORIGINAL_SKIPPED, _ORIGINAL_FAILED, STATUS_MOVING):
        counts: Dict[str, int] = {}
        ordered: List[Tuple[str, str]] = []
        for entry in entries:
            if entry.get("status") != status:
                continue
            root = _root_of(entry.get("dst") or "")
            if not root:
                continue
            key = os.path.normcase(os.path.abspath(root))
            if key not in counts:
                counts[key] = 0
                ordered.append((key, root))
            counts[key] += 1

        if counts:
            # max 在计数相同时会保留先出现的那个，结果稳定可复现
            return max(ordered, key=lambda pair: counts[pair[0]])[1]

    return ""


def list_batches(store: RecordStore) -> List[BatchSummary]:
    """列出所有**原始整理批次**，按时间倒序。

    撤销批次（带 undo_of）不会出现在结果里——否则用户会在"历史记录"里
    看到一堆自己没整理过的批次，越用越乱。
    """
    originals, restored = _split_records(store.read_all())

    summaries: List[BatchSummary] = []
    for batch_id, entries in originals.items():
        moved = skipped = failed = uncertain = 0
        moved_pairs: List[Tuple[str, str]] = []

        for entry in entries:
            status = entry.get("status")
            if status == _MOVED:
                moved += 1
                src = entry.get("src") or ""
                dst = entry.get("dst") or ""
                if src:
                    moved_pairs.append((src, dst))
            elif status == _ORIGINAL_SKIPPED:
                skipped += 1
            elif status == _ORIGINAL_FAILED:
                failed += 1
            elif status == STATUS_MOVING:
                uncertain += 1

        # 可撤销数按**磁盘现状**算，不只看记录：
        #   ① 撤销到一半断电时，文件已经回原位、但撤销记录没写成，
        #      只看记录会显示"可撤销 1 个"，点下去却只能跳过；
        #   ② 用户可能自己把分类文件夹里的文件挪走/删掉了。
        # 加一次 isfile 判断（本地磁盘开销很小），界面上的数字才和实际能做的事一致。
        undoable = sum(
            1
            for src, dst in moved_pairs
            if (batch_id, src) not in restored and dst and os.path.isfile(dst)
        )
        stamps = sorted(e.get("ts") or "" for e in entries if e.get("ts"))
        started_at = stamps[0] if stamps else ""

        summaries.append(
            BatchSummary(
                batch_id=batch_id,
                started_at=started_at,
                root=_root_from_entries(entries),
                moved=moved,
                skipped=skipped,
                failed=failed,
                uncertain=uncertain,
                undoable=undoable,
            )
        )

    summaries.sort(key=lambda s: (s.started_at, s.batch_id), reverse=True)
    return summaries


# ====================================================================== 检查
def _path_problem(root: str, src: str, dst: str) -> str:
    """路径层面的校验：记录里的路径是不是还能组成"一次合法的整理"。

    返回问题描述，空字符串表示通过。
    这里的每一条都基于 realpath 判断，记录被人改过、或者中间被换成目录联接，
    都会被拦下来。
    """
    if not root:
        return "记录里缺少整理目录信息，已跳过"
    if not src or not dst:
        return "记录缺少路径信息，已跳过"

    if os.path.normcase(os.path.basename(src)) != os.path.normcase(os.path.basename(dst)):
        return "记录中的文件名不一致，已跳过"

    # 原始文件必须在整理目录的第一层（整理只会动第一层的文件）
    if not is_direct_child(root, src):
        return "原始位置不在整理目录内，已跳过（记录可能被改动）"

    # 分类文件夹必须是整理目录的直接子目录
    if not is_direct_child(root, os.path.dirname(dst)):
        return "分类文件夹不在整理目录内，已跳过（记录可能被改动）"

    if not is_inside(root, dst):
        return "当前路径不在整理目录内，已跳过（记录可能被改动）"

    return ""


def _state_problem(src: str, dst: str) -> str:
    """磁盘现状的校验：文件现在到底在不在、能不能安全搬回去。"""
    if not os.path.exists(dst):
        return "目标文件已不存在（可能被移动或删除）"
    # 和 mover 的规则保持一致：链接指向的可能是目录外面的文件，
    # 把链接搬回去等于动了一个我们没整理过的东西，所以一律跳过。
    if os.path.islink(dst):
        return "目标路径是快捷方式或链接，已跳过"
    if not os.path.isfile(dst):
        return "目标路径不是普通文件，已跳过"
    # 用 lexists：悬空链接在 exists 眼里"不存在"，但它确实占了名字，rename 会失败
    if os.path.lexists(src):
        return "原始位置已有同名文件，已跳过（不覆盖）"
    if not os.path.isdir(os.path.dirname(src)):
        return "原始文件夹已不存在"
    return ""


def _already_undone_message() -> str:
    return REASON_ALREADY_UNDONE


# ====================================================================== 生成方案
def build_undo_plan(store: RecordStore, batch_id: str) -> UndoPlan:
    """为某个批次生成撤销方案。

    只做检查、**不动任何文件**。检查不过的条目照样留在 items 里，
    只是状态为 skipped 并带上原因——界面要把"为什么不撤销"明确告诉用户。
    """
    originals, restored = _split_records(store.read_all())
    entries = originals.get(batch_id, [])
    root = _root_from_entries(entries)

    items: List[UndoItem] = []

    for entry in entries:
        status = entry.get("status")
        src = entry.get("src") or ""
        dst = entry.get("dst") or ""
        category = entry.get("category") or ""

        # ---- 状态不确定：上次整理中途断了，绝不能当成"搬走了"处理 ----
        if status == STATUS_MOVING:
            items.append(
                UndoItem(src, dst, category, STATUS_SKIPPED,
                         "上次整理未正常结束，状态不确定，已跳过")
            )
            continue

        # ---- 整理时就没搬走的（跳过/失败）不进撤销列表 ----
        if status != _MOVED:
            continue

        # ---- 同批次里目录不一致：记录被拼接过，整条不碰 ----
        own_root = _root_of(dst)
        if root and own_root and os.path.normcase(os.path.abspath(own_root)) != os.path.normcase(os.path.abspath(root)):
            items.append(
                UndoItem(src, dst, category, STATUS_SKIPPED, "记录中的目录不一致，已跳过")
            )
            continue

        problem = _path_problem(root, src, dst)
        if problem:
            items.append(UndoItem(src, dst, category, STATUS_SKIPPED, problem))
            continue

        # 先判断"是不是已经撤销过"，再看磁盘现状：
        # 撤销过的文件本来就已经不在分类文件夹里了，如果先查 dst 是否存在，
        # 用户只会看到"目标文件已不存在"，看不出真正的原因是"已经撤销过"。
        if (batch_id, src) in restored:
            items.append(
                UndoItem(src, dst, category, STATUS_SKIPPED, _already_undone_message())
            )
            continue

        problem = _state_problem(src, dst)
        if problem:
            items.append(UndoItem(src, dst, category, STATUS_SKIPPED, problem))
            continue

        items.append(UndoItem(src, dst, category, STATUS_READY, "可以恢复"))

    # 先恢复更深的位置，再恢复上层：这样即使以后支持嵌套目录，也不会因为
    # 父目录先被占用而失败；同级按记录原始顺序倒序，行为稳定可复现。
    indexed = list(enumerate(items))
    indexed.sort(key=lambda pair: (_depth(pair[1].dst), pair[0]), reverse=True)
    items = [item for _, item in indexed]

    return UndoPlan(batch_id=batch_id, root=root, items=items)


# ====================================================================== 执行撤销
def _undo_one(item: UndoItem, root: str, restored: Set[Tuple[str, str]], batch_id: str) -> UndoItem:
    """恢复一个文件。任何异常都转成结果，**不往外抛**。

    注意：这里重新做了一遍和 build 阶段完全相同的检查——
    生成方案到用户点确认之间可能过了几分钟，磁盘上什么都可能发生。
    """
    problem = _path_problem(root, item.src, item.dst)
    if problem:
        return UndoItem(item.src, item.dst, item.category, STATUS_SKIPPED, problem)

    # 同一次撤销里已经处理过、或者历史里已有 restored 记录 → 不重复搬
    if (batch_id, item.src) in restored:
        return UndoItem(item.src, item.dst, item.category, STATUS_SKIPPED,
                        _already_undone_message())

    problem = _state_problem(item.src, item.dst)
    if problem:
        return UndoItem(item.src, item.dst, item.category, STATUS_SKIPPED, problem)

    # 记录移动前的大小，搬完比对，确认内容没被截断
    try:
        size_before = os.path.getsize(item.dst)
    except OSError as exc:
        return UndoItem(item.src, item.dst, item.category, STATUS_FAILED,
                        f"读取文件大小失败：{exc.strerror or exc}")

    try:
        _move_file(item.dst, item.src)
    except FileExistsError:
        # 检查和移动之间被别人抢先建了同名文件：照样跳过，绝不覆盖
        return UndoItem(item.src, item.dst, item.category, STATUS_SKIPPED,
                        "原始位置刚出现同名文件，已跳过（不覆盖）")
    except PermissionError as exc:
        return UndoItem(item.src, item.dst, item.category, STATUS_FAILED, _permission_text(exc))
    except OSError as exc:
        return UndoItem(item.src, item.dst, item.category, STATUS_FAILED, _os_error_text(exc))

    # ---- 搬完再核对一次 ----
    # 文件已经回到原位置了，这里即使校验不过也只能记 restored（+ 需人工确认）：
    # 记成 failed 会让用户以为文件还留在分类文件夹里，反而更乱。
    if not os.path.isfile(item.src):
        return UndoItem(item.src, item.dst, item.category, STATUS_RESTORED,
                        "已恢复，但原始位置的文件状态异常，请手动检查", verified=False)
    try:
        if os.path.getsize(item.src) != size_before:
            return UndoItem(item.src, item.dst, item.category, STATUS_RESTORED,
                            "已恢复，但文件大小与移动前不一致，请手动检查", verified=False)
    except OSError:
        # 读不到大小不算失败：文件确实已经在原位置了
        pass

    return UndoItem(item.src, item.dst, item.category, STATUS_RESTORED, "已恢复")


def execute_undo(
    plan: UndoPlan,
    store: Optional[RecordStore] = None,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> UndoReport:
    """按方案执行撤销，返回汇总报告。

    参数：
        store         —— 操作记录仓库，None 表示不记录
        on_progress   —— 每处理完一个文件回调 on_progress(done, total)
        should_cancel —— 返回 True 表示用户要停止，剩下的不再处理

    这个函数不弹任何界面，也不抛异常：单个文件的任何问题都收进 results。
    """

    report = UndoReport(undo_id=new_undo_id(), batch_id=plan.batch_id)
    ready = [item for item in plan.items if item.status == STATUS_READY]
    report.total = len(ready)
    total = report.total

    # 执行前重读一遍撤销记录：生成方案之后可能已经撤销过一次了
    restored: Set[Tuple[str, str]] = set()
    if store is not None:
        try:
            _, restored = _split_records(store.read_all())
        except RecordError:
            # 读不到记录不等于可以乱来：按"什么都没撤销过"继续，
            # 后面的路径/覆盖检查仍然能兜住，只是可能重复处理一条
            restored = set()

    retry_entries: List[Dict] = []      # 记录写失败的，收尾时再试一次
    missing: Set[str] = set()

    for index, item in enumerate(ready, start=1):
        # 用户点了停止：立刻收尾，不再碰任何文件
        if should_cancel is not None and should_cancel():
            report.stopped = True
            break

        try:
            result = _undo_one(item, plan.root, restored, plan.batch_id)
        except Exception as exc:         # noqa: BLE001 —— 兜底：单文件异常不能带崩整批
            result = UndoItem(item.src, item.dst, item.category, STATUS_FAILED,
                              f"意外错误：{exc}")

        report.results.append(result)
        if result.status == STATUS_RESTORED and result.src:
            restored.add((plan.batch_id, result.src))

        # ---- 每个文件写一条撤销记录：撤销本身也要留痕 ----
        if store is not None:
            entry = {
                "ts": now_text(),
                "batch_id": report.undo_id,
                "undo_of": plan.batch_id,
                "phase": PHASE_UNDO,
                "status": result.status,
                "src": result.src,
                "dst": result.dst,
                "category": result.category,
                "message": result.message,
            }
            try:
                store.append(entry)
            except RecordError:
                retry_entries.append(entry)
                missing.add(result.src)

        if on_progress is not None:
            try:
                on_progress(index, total)
            except Exception:            # noqa: BLE001 —— 界面回调出错不能影响撤销
                pass

    # ---- 收尾：把写失败的记录再试一次 ----
    if store is not None and retry_entries:
        for entry in retry_entries:
            try:
                store.append(entry)
            except RecordError:
                continue
            missing.discard(entry.get("src"))

    report.record_errors = len(missing)
    report.record_failed_items = sorted(x for x in missing if x)
    return report
