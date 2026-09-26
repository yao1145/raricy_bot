# Light 冻结配置（LIGHT_EDITION_DESIGN §15.1）：onedir + windowed（无终端）。
# 由 tools/build_light.py 复制到 staging 根后执行；所有路径相对 staging。
#
# `hiddenimports` 只列运行时**动态导入**的模块：uvicorn 的协议/循环实现与
# keyring 的系统后端都不是静态 import，PyInstaller 的静态分析看不到它们。
a = Analysis(
    ["entry_light.py"],
    pathex=["src"],
    binaries=[],
    # 静态资源随包：发行程序从包资源定位页面，不依赖运行期 Node 或 CDN（§3.1）。
    # 托盘图标同法随包（N3）：PyInstaller 只收集这里列出的路径，缺一项发行包就少一个
    # 文件（staging 里有、冻结产物里没有）。此列表与 packaging/light/pyproject.toml 的
    # package-data 必须同步，见 INTERFACES.md §60。
    datas=[
        ("src/raricy_launcher/static", "raricy_launcher/static"),
        ("src/raricy_launcher/assets", "raricy_launcher/assets"),
    ],
    hiddenimports=[
        "win32timezone",
        "uvicorn.logging",
        "uvicorn.loops",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        "uvicorn.protocols",
        "uvicorn.protocols.http",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.websockets",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.lifespan",
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        # keyring 是**惰性导入**（在方法体里），静态分析看不到；不列它的话冻结包
        # 里根本没有 keyring，保存凭据会永远回 503（审查 F3）。
        "keyring",
        "keyring.backends.Windows",
        "keyring.backends.null",
    ],
    hooksconfig={},
    runtime_hooks=[],
    # 发行包不含 MCP SDK 与工具实现（§4.2）；静态检查另有 staging 白名单把关。
    # 其余排除项是**构建环境里存在、发行包不需要**的科学计算/GUI/测试栈：不排除
    # 的话它们会被整包带走（实测 llvmlite 一项就有 100 MB，PyQt5 与 numpy/scipy
    # 另占 70 MB 以上）。新增排除项前先确认没有运行时导入。
    excludes=[
        "mcp",
        "raricy_bot.mcp",
        "raricy_bot.blog",
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "llvmlite",
        "numba",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
        "PIL",
        "IPython",
        "notebook",
        "pytest",
        "sphinx",
        "docutils",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="RaricyBotLight",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="RaricyBotLight")
