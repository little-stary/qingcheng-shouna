# -*- coding: utf-8 -*-
"""整理方案生成器：算出「每个文件该去哪个文件夹」。

★ 本阶段最重要的一条：这个模块只做计算，**不创建文件夹、不移动文件**。
  判断"目标是否已存在"用的是 os.path.exists，属于只读操作。

方案结构：
    D:\\下载\\a.pdf  ->  D:\\下载\\文档\\a.pdf
                      ^ 目标目录 = 用户选中的目录 + 分类文件夹名
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List, Sequence

from app.config import CATEGORY_FOLDER, CATEGORY_ORDER
from app.core.models import FileItem
from app.utils.paths import is_inside


@dataclass
class PlanItem:
    """方案里的一条：一个文件 + 它的建议去处。"""

    src: str                       # 原文件完整路径
    name: str                      # 文件名
    category: str                  # 分类
    target_dir: str                # 建议的目标文件夹
    dst: str                       # 建议的完整目标路径
    conflict: bool = False         # 目标位置是否已存在同名东西
    conflict_kind: str = ""        # 冲突类型：同名文件 / 同名文件夹 / 目标不合法
    inside: bool = True            # 目标是否落在所选目录内部

    @property
    def status_text(self) -> str:
        if not self.inside:
            return "目标越界"
        if self.conflict:
            return self.conflict_kind or "冲突"
        return "可整理"


@dataclass
class Plan:
    """一次完整的整理方案。"""

    source_dir: str
    items: List[PlanItem] = field(default_factory=list)
    new_folders: List[str] = field(default_factory=list)     # 方案里需要新建、但尚未创建的目录
    blocked_folders: Dict[str, str] = field(default_factory=dict)  # 分类 -> 不能用的原因

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def conflict_count(self) -> int:
        return sum(1 for i in self.items if i.conflict or not i.inside)

    @property
    def conflict_items(self) -> List[PlanItem]:
        return [i for i in self.items if i.conflict or not i.inside]

    def count_by_category(self) -> Dict[str, int]:
        counts = {c: 0 for c in CATEGORY_ORDER}
        for item in self.items:
            counts[item.category] = counts.get(item.category, 0) + 1
        return counts


def _check_target_dir(source_dir: str, category: str) -> tuple[str, str]:
    """算出某个分类的目标文件夹，并判断它能不能用。

    返回 (目标目录, 错误原因)。错误原因为空字符串表示可用。

    这里处理了一个容易被忽略的情况：
        如果目录里恰好有一个叫「图片」的**文件**，那「图片」文件夹就建不了，
        这一类的文件全部不能整理——必须提前告诉用户，而不是等到移动时才报错。
    """
    folder_name = CATEGORY_FOLDER.get(category, category)
    target_dir = os.path.join(source_dir, folder_name)

    if os.path.exists(target_dir) and not os.path.isdir(target_dir):
        return target_dir, f"目标名被同名文件占用，无法建立「{folder_name}」文件夹"

    return target_dir, ""


def build_plan(files: Sequence[FileItem], source_dir: str) -> Plan:
    """根据扫描结果生成整理方案。

    参数：
        files      —— 扫描得到的文件列表（FileItem）
        source_dir —— 用户选中的文件夹

    返回：
        Plan 对象。**过程中不会创建任何文件夹，也不会动任何文件。**
    """
    source_dir = os.path.abspath(source_dir)
    plan = Plan(source_dir=source_dir)

    # 1) 先看这次到底用到了哪些分类：没有文件的分类不该建文件夹
    used_categories = {f.category for f in files}

    target_dirs: Dict[str, str] = {}
    for category in CATEGORY_ORDER:
        target_dir, error = _check_target_dir(source_dir, category)
        target_dirs[category] = target_dir
        if category not in used_categories:
            continue                      # 这一类没有文件，不建目录也不报错
        if error:
            plan.blocked_folders[category] = error
        elif not os.path.exists(target_dir):
            plan.new_folders.append(target_dir)

    # 2) 逐个文件算目标路径 + 检查冲突
    for f in files:
        target_dir = target_dirs.get(f.category, os.path.join(source_dir, f.category))
        dst = os.path.join(target_dir, f.name)

        item = PlanItem(
            # 统一转成绝对路径：扫描时如果传的是相对路径，这里存的也是相对路径，
            # 写进操作记录后一旦换工作目录就找不到原文件，撤销会失效。
            src=os.path.abspath(f.path),
            name=f.name,
            category=f.category,
            target_dir=target_dir,
            dst=dst,
            inside=is_inside(source_dir, dst),
        )

        # 目标越界属于严重问题，标记后不再判断冲突
        if not item.inside:
            item.conflict = True
            item.conflict_kind = "目标越过所选目录"
        elif f.category in plan.blocked_folders:
            item.conflict = True
            item.conflict_kind = "目标文件夹不可用"
        elif os.path.exists(dst):
            item.conflict = True
            # 同名的是文件夹还是文件，提示语不一样
            item.conflict_kind = "同名文件夹" if os.path.isdir(dst) else "同名文件"

        plan.items.append(item)

    # 3) 按分类顺序、再按文件名排序，预览时更好读
    order = {name: index for index, name in enumerate(CATEGORY_ORDER)}
    plan.items.sort(key=lambda i: (order.get(i.category, 99), i.name.lower()))

    return plan
