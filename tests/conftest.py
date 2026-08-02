"""pytest 共享配置。

裸 `pytest` 默认只把测试文件所在目录（tests/）加入 sys.path，
导致 `import core.*` 失败。此 conftest 把仓库根目录加入 sys.path，
使测试可用标准命令 `pytest` 或 `python -m pytest` 从仓库根目录运行。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
