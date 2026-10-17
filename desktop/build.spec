# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir，配合 Inno Setup 出安装包）。

用法（在项目根）：
    .venv\\Scripts\\pyinstaller.exe desktop\\build.spec --noconfirm

产物：
    dist\\PersonalAgent\\PersonalAgent.exe
"""

import os
import sys

from PyInstaller.utils.hooks import collect_submodules

# 关键：spec 执行时 sys.path 里没有项目根，collect_submodules("src")
# 会静默返回空列表 —— 打出来的包缺整个 src 包，运行时报
# ModuleNotFoundError: No module named 'src'。
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(SPEC), ".."))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 前端构建产物 → 解包目录下的 frontend_dist（src/main.py 会去这里找）
# 路径相对 spec 所在目录（desktop/），所以是 ../frontend/dist
datas = [
    ("../frontend/dist", "frontend_dist"),
]

# 动态 import 的模块（autodetect 抓不全）
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("edge_tts")
    + collect_submodules("webview")
    + collect_submodules("apscheduler")
    + collect_submodules("src")
)

block_cipher = None

a = Analysis(
    ["run_desktop.py"],          # 相对 spec 所在目录（desktop/）
    pathex=[".."],
    binaries=[],
    datas=[("../frontend/dist", "frontend_dist")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # src 完全用不到这些：transformers/sentence-transformers 是之前
        # 试知识库留下的，打包时会连带把 torch（几百 MB）拖进来
        "torch",
        "torchvision",
        "transformers",
        "sentence_transformers",
        "huggingface_hub",
        "safetensors",
        "tokenizers",
        "numpy",
        "scipy",
        "pandas",
        "matplotlib",
        "PIL",
        "tkinter",
        "pytest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PersonalAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT_ROOT, "desktop", "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PersonalAgent",
)
