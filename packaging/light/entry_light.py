"""冻结发行入口：PyInstaller 以此脚本为程序入口。

与 `python -m raricy_launcher` 等价；`--worker` 参数经同一入口分派为
Worker 子进程（LIGHT_EDITION_DESIGN §10.1）。
"""

from raricy_launcher.main import main

raise SystemExit(main())
