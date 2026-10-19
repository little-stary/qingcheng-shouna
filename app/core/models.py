# -*- coding: utf-8 -*-
"""数据模型：扫描和整理过程中传递的数据。

新手提示：这里只定义"数据长什么样"，不写任何逻辑。
好处是界面和逻辑都按同一份结构交换数据，谁都不用猜。
"""

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class FileItem:
    """一个待整理的文件。"""

    path: str          # 完整路径，例如 D:\\下载\\报告.pdf
    name: str          # 文件名，例如 报告.pdf
    ext: str           # 扩展名（小写，带点），例如 .pdf
    size: int          # 大小，单位字节
    mtime: float       # 最后修改时间（时间戳）
    category: str      # 分类结果，例如 文档


@dataclass
class ScanResult:
    """一次扫描的完整结果。"""

    folder: str                                  # 被扫描的文件夹
    files: List[FileItem] = field(default_factory=list)
    sub_folder_count: int = 0                    # 第一层的子文件夹数量（不会被整理）
    skipped: List[str] = field(default_factory=list)   # 因权限或长度读不到的文件名
    truncated: bool = False                      # 是否因为文件太多而提前停止
    cancelled: bool = False                      # 是否被用户中途取消

    # ---- 下面是"算出来的"统计，界面直接拿来用 ----

    @property
    def total_files(self) -> int:
        return len(self.files)

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    def count_by_category(self) -> Dict[str, int]:
        """返回 {分类: 文件个数}。"""
        result = {c: 0 for c in self.count_order()}
        for f in self.files:
            result[f.category] = result.get(f.category, 0) + 1
        return result

    def size_by_category(self) -> Dict[str, int]:
        """返回 {分类: 总字节数}。"""
        result = {c: 0 for c in self.count_order()}
        for f in self.files:
            result[f.category] = result.get(f.category, 0) + f.size
        return result

    @staticmethod
    def count_order() -> List[str]:
        # 延迟导入，避免 models 反向依赖 config 造成隐式循环
        from app.config import CATEGORY_ORDER
        return CATEGORY_ORDER
