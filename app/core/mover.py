# -*- coding: utf-8 -*-
"""执行整理：把方案里的文件真正搬到分类文件夹。

安全规则（每一条都对应需求里的一条要求）：
    1. 只有用户点了「确认整理」才会走到这里；
    2. 每个文件搬之前都**重新检查一遍**，不信任预览时算出的旧结果；
    3. 源文件必须在所选目录的**第一层**，目标必须落在所选目录内部，
       链接（符号链接/目录联接）解析后仍然要在里面；
    4. 目标已存在同名文件/文件夹 → 跳过，绝不覆盖；
    5. 源文件不见了/不是普通文件/是链接 → 跳过；
    6. 单个文件失败只记录，不抛出，不影响其它文件；
    7. 全程不删除任何文件；
    8. 移动前后各写一条记录，断电也能知道文件去哪了。
"""

import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from app.core.planner import Plan, PlanItem
from app.core.records import STATUS_MOVING, RecordError, RecordStore, now_text
from app.utils.paths import is_direct_child, is_inside

STATUS_MOVED = "moved"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

STATUS_TEXT = {
    STATUS_MOVED: "已移动",
    STATUS_SKIPPED: "已跳过",
    STATUS_FAILED: "失败",
}


@dataclass
class MoveResult:
    """单个文件的执行结果。"""

    name: str
    src: str
    dst: str
    category: str
    status: str                    # moved / skipped / failed
    message: str = ""
    verified: bool = True          # 移动后的校验是否通过

    @property
    def ok(self) -> bool:
        return self.status == STATUS_MOVED


@dataclass
class MoveReport:
    """一次整理任务的汇总。"""

    batch_id: str
    results: List[MoveResult] = field(default_factory=list)
    created_dirs: List[str] = field(default_factory=list)
    stopped: bool = False
    record_errors: int = 0         # 有几个文件的结果记录没写进文件
    record_failed_items: List[str] = field(default_factory=list)   # 上面这些文件的源路径
    total: int = 0                 # 计划里一共多少个文件

    @property
    def moved(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_MOVED)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_SKIPPED)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == STATUS_FAILED)

    @property
    def unverified(self) -> int:
        """已经搬走、但事后校验没通过的文件数（需要用户自己确认）。"""
        return sum(1 for r in self.results if r.ok and not r.verified)

    @property
    def remaining(self) -> int:
        """因为用户停止而没来得及处理的文件数。"""
        return max(0, self.total - len(self.results))

    def summary_text(self) -> str:
        parts = [f"成功 {self.moved} 个"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 个")
        if self.failed:
            parts.append(f"失败 {self.failed} 个")
        if self.unverified:
            parts.append(f"其中 {self.unverified} 个需人工确认")
        if self.stopped:
            parts.append(f"因停止未处理 {self.remaining} 个")
        return "，".join(parts)


def new_batch_id() -> str:
    """一次整理任务一个批次号，撤销时按批次找回。"""
    return uuid.uuid4().hex[:12]


def _skip(item: PlanItem, message: str) -> MoveResult:
    return MoveResult(item.name, item.src, item.dst, item.category, STATUS_SKIPPED, message)


def _fail(item: PlanItem, message: str) -> MoveResult:
    return MoveResult(item.name, item.src, item.dst, item.category, STATUS_FAILED, message)


def _bad_name(name: str) -> bool:
    """文件名里不允许出现路径分隔符，否则拼接出来的目标可能跑到别的目录。"""
    if not name or name in (".", ".."):
        return True
    return "/" in name or "\\" in name


def _permission_text(exc: PermissionError) -> str:
    """把 Windows 的"拒绝访问"细分成"被占用"和"没权限"两种情况。"""
    code = getattr(exc, "winerror", None)
    if code in (32, 33):            # ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION
        return "文件正被其他程序占用，已跳过（关闭该文件后重试）"
    if code == 5:                   # ERROR_ACCESS_DENIED
        return "没有权限修改这个文件或文件夹，已跳过"
    return "没有权限，或文件正被其他程序占用，已跳过"


def _os_error_text(exc: OSError) -> str:
    """把常见的系统错误翻译成用户能看懂的话。"""
    code = getattr(exc, "winerror", None)
    err = getattr(exc, "errno", None)

    if err in (28, 122) or code == 112:      # ENOSPC / ERROR_DISK_FULL
        return "磁盘空间不足，已跳过"
    if err == 18 or code == 17:               # EXDEV / ERROR_NOT_SAME_DEVICE
        return "不支持跨磁盘移动，已跳过"
    if code == 206 or err == 36:              # ERROR_FILENAME_EXCED_RANGE
        return "目标路径过长，Windows 无法处理"

    text = str(exc).lower()
    if "space" in text:
        return "磁盘空间不足，已跳过"
    if "too long" in text:
        return "目标路径过长，Windows 无法处理"

    return f"移动失败：{exc.strerror or exc}"


def _move_file(src: str, dst: str) -> None:
    """把 src 移到 dst，**保证不覆盖**已经存在的 dst。

    为什么不用 shutil.move：
        shutil.move 在目标恰好是**文件夹**时，会把文件塞进那个文件夹里
        （变成 dst\\文件名），实际落点和记录下来的目标路径不一致，
        撤销时就找不到文件了。
        os.rename 在 Windows 上只要目标已存在（文件或文件夹）就直接报错，
        既不会覆盖，也不会悄悄改变去处。

    为什么不做跨盘复制：
        目标文件夹永远是所选目录的子文件夹，和源文件同一个卷；
        真出现跨盘说明路径被改过，这时宁可失败也不能靠"复制+删除"去冒险
        （复制一半失败会留下半成品，删除源文件更是不可接受）。

    非 Windows 系统：os.rename 会静默覆盖目标，所以直接拒绝执行——
    宁可不能用，也不能弄坏文件。
    """
    if os.name != "nt":
        raise OSError("清爽收纳目前只支持 Windows，已拒绝在其他系统上移动文件")
    os.rename(src, dst)


def move_one(item: PlanItem, source_dir: str, created_dirs: Optional[List[str]] = None) -> MoveResult:
    """移动一个文件。任何异常都转成结果，不往外抛。"""

    # --- 检查 1：目标必须落在所选目录内部（链接解析后仍要在里面）---
    if not item.inside or not is_inside(source_dir, item.dst):
        return _skip(item, "目标路径不在所选目录内，已跳过")

    # --- 检查 2：源文件必须在所选目录的第一层 ---
    # 光看 src 存不存在不够：如果方案被改过，src 可能指向 C:\\Windows 里的文件。
    if not is_inside(source_dir, item.src):
        return _skip(item, "源文件不在所选目录内，已跳过")
    if not is_direct_child(source_dir, item.src):
        return _skip(item, "源文件不在所选目录的第一层，已跳过")

    # --- 检查 3：文件名必须干净，不能带路径分隔符 ---
    if _bad_name(item.name):
        return _skip(item, "文件名不合法，已跳过")

    # --- 检查 4：源文件还在不在 ---
    if os.path.islink(item.src):
        # 链接指向的可能是目录外面的文件，动它超出了"整理这个文件夹"的范围
        return _skip(item, "源路径是快捷方式或链接，已跳过")
    if not os.path.exists(item.src):
        return _skip(item, "源文件已不存在（可能在预览之后被移动或删除）")
    if not os.path.isfile(item.src):
        return _skip(item, "源路径不是普通文件，已跳过")

    # --- 检查 5：目标是否已有同名东西（绝不覆盖）---
    # 用 lexists 而不是 exists：悬空链接在 exists 眼里是"不存在"，
    # 但它确实占了那个名字，rename 会失败，提前判掉能给出准确原因。
    if os.path.lexists(item.dst):
        kind = "文件夹" if os.path.isdir(item.dst) else "文件或链接"
        return _skip(item, f"目标位置已有同名{kind}，已跳过（不覆盖）")

    target_dir = os.path.dirname(item.dst)

    # --- 检查 6：目标文件夹能不能建出来 ---
    if not os.path.isdir(target_dir):
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            return _fail(item, f"无法创建目标文件夹：{exc.strerror or exc}")

        # 建完再确认一次：万一这一瞬间有人把它换成了指向别处的链接
        if not is_direct_child(source_dir, target_dir):
            return _fail(item, "目标文件夹指向了所选目录之外，已停止")

        if created_dirs is not None:
            created_dirs.append(target_dir)

    # 记录移动前的大小，移完比对，确认内容没被截断
    try:
        size_before = os.path.getsize(item.src)
    except OSError:
        size_before = None

    # --- 执行移动 ---
    try:
        _move_file(item.src, item.dst)
    except FileExistsError:
        # 检查和移动之间被别人抢先建了同名文件：照样跳过，不覆盖
        return _skip(item, "目标位置刚出现同名文件，已跳过（不覆盖）")
    except PermissionError as exc:
        return _fail(item, _permission_text(exc))
    except OSError as exc:
        return _fail(item, _os_error_text(exc))

    # --- 移动后再检查一遍 ---
    # 注意：文件已经搬走了，这里就算校验不通过，也**只能记成"已移动但需确认"**。
    # 记成"失败"会让用户以为文件还在原地，撤销功能也会漏掉这个文件。
    if not os.path.isfile(item.dst):
        return MoveResult(
            item.name, item.src, item.dst, item.category, STATUS_MOVED,
            "已移动，但目标文件状态异常，请手动检查", verified=False,
        )
    if size_before is not None:
        try:
            if os.path.getsize(item.dst) != size_before:
                return MoveResult(
                    item.name, item.src, item.dst, item.category, STATUS_MOVED,
                    "已移动，但文件大小与原文件不一致，请手动检查", verified=False,
                )
        except OSError:
            pass

    return MoveResult(item.name, item.src, item.dst, item.category, STATUS_MOVED, "已移动")


def execute_plan(
    plan: Plan,
    on_progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    store: Optional[RecordStore] = None,
    batch_id: Optional[str] = None,
) -> MoveReport:
    """按方案执行整理，返回汇总报告。

    参数：
        on_progress(done, total) —— 每处理完一个文件回调一次
        should_cancel()          —— 返回 True 表示用户要停止，剩下的不再处理
        store                    —— 记录仓库，None 表示不记录（测试时用）

    注意：这个函数**不弹任何界面**，全部结果通过返回值汇报，
    这样界面层怎么显示都不会影响移动逻辑本身。
    """

    report = MoveReport(batch_id=batch_id or new_batch_id(), total=len(plan.items))
    total = len(plan.items)

    retry_entries: List[dict] = []      # 结果记录写失败的，收尾时再试一次
    missing_end = set()                 # 结果记录最终没能落盘的文件（源路径）

    for index, item in enumerate(plan.items, start=1):
        # 用户点了停止：立刻收尾，不再碰任何文件
        if should_cancel is not None and should_cancel():
            report.stopped = True
            break

        # ---- 先写"准备移动"：移动完就断电也还能查到文件本来要去哪 ----
        if store is not None:
            begin_entry = {
                "ts": now_text(),
                "batch_id": report.batch_id,
                "phase": "begin",
                "status": STATUS_MOVING,
                "src": item.src,
                "dst": item.dst,
                "category": item.category,
                "message": "准备移动",
            }
            try:
                store.append(begin_entry)
            except RecordError:
                # 不重试：万一重试成功但排在结果记录后面，会被当成"未完成"。
                # 结果记录本身包含全部信息，丢一条 begin 不影响撤销。
                pass

        try:
            result = move_one(item, plan.source_dir, report.created_dirs)
        except Exception as exc:                 # noqa: BLE001 —— 兜底：单文件异常不能带崩整批
            result = _fail(item, f"意外错误：{exc}")

        report.results.append(result)

        # ---- 再写真实结果：成功/跳过/失败都记，撤销时只认 moved ----
        if store is not None:
            end_entry = {
                "ts": now_text(),
                "batch_id": report.batch_id,
                "phase": "end",
                "status": result.status,
                "src": result.src,
                "dst": result.dst,
                "category": result.category,
                "message": result.message,
            }
            try:
                store.append(end_entry)
            except RecordError:
                retry_entries.append(end_entry)
                missing_end.add(result.src)

        if on_progress is not None:
            try:
                on_progress(index, total)
            except Exception:                # noqa: BLE001 —— 界面回调出错不能影响整理
                pass

    # ---- 收尾：把写失败的结果记录再试一次 ----
    # 磁盘短暂被占用、杀毒软件锁文件这类失败往往是瞬时的。
    # 不重试的话，文件明明已经移动成功，记录里却永远停在"准备移动"。
    if store is not None and retry_entries:
        for entry in retry_entries:
            try:
                store.append(entry)
            except RecordError:
                continue
            missing_end.discard(entry.get("src"))

    report.record_errors = len(missing_end)
    report.record_failed_items = sorted(x for x in missing_end if x)
    return report
