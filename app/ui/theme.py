# -*- coding: utf-8 -*-
"""界面主题：字体和间距的小工具，集中放一处，改起来方便。

关于缩放（踩过的坑，记在这里免得再犯）：
    Tk 里所有尺寸单位都是"逻辑像素"，Windows 会把它们和字体一起按系统缩放放大。
    所以 padding 直接写 12 就行，**不要**再手动乘缩放比例——乘了会变成双重放大，
    界面会被撑到装不下。CustomTkinter 内部对控件尺寸也是同样的处理。
"""

from app.config import FONT_FAMILY

def font(size: int, bold: bool = False):
    """返回 CustomTkinter 能直接用的字体元组。

    Windows 上 "Microsoft YaHei UI" 是系统自带中文字体，
    显示中文比默认字体清晰，也不需要额外安装。
    """
    return (FONT_FAMILY, size, "bold") if bold else (FONT_FAMILY, size)


PAD_CARD = 12        # 卡片内边距
GAP_CARD = 10        # 卡片之间的间距
