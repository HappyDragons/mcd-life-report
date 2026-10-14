"""`tests` 包：全部测试只使用 Python 标准库 ``unittest``。

运行方式（在仓库根目录）：

    python -m unittest discover tests -v
"""

from __future__ import annotations

import os
import sys

# 保证无论以何种方式启动测试，仓库根目录都在 import 路径上。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
