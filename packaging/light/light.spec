# Light 冻结配置原型：onedir + windowed（无终端，§15.1）。
# 由 tools/build_light.py 复制到 staging 根后执行；所有路径相对 staging。
a = Analysis(
    ["entry_light.py"],
    pathex=["src"],
    binaries=[],
    datas=[("src/raricy_launcher/static", "raricy_launcher/static")],
    hiddenimports=["win32timezone"],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["mcp"],
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
