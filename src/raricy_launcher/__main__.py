"""模块入口：``python -m raricy_launcher``，转调 `main.main()`。"""

from __future__ import annotations

import sys

from .main import main

if __name__ == "__main__":
    sys.exit(main())
