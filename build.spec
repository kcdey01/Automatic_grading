# -*- mode: python ; coding: utf-8 -*-
import os
import sys
import glob as _glob
from PyInstaller.utils.hooks import collect_all

block_cipher = None

datas = []
binaries = []
hiddenimports = []

# 收集 PIL 的所有文件
pil_datas, pil_binaries, pil_hiddenimports = collect_all('PIL')
datas += pil_datas
binaries += pil_binaries
hiddenimports += pil_hiddenimports

# 收集 pyautogui
pa_datas, pa_binaries, pa_hiddenimports = collect_all('pyautogui')
datas += pa_datas
binaries += pa_binaries
hiddenimports += pa_hiddenimports

# Anaconda 环境下 PyInstaller 无法自动发现 Library/bin 中的 DLL
# 将 base_prefix/Library/bin 下所有 DLL 一并打包，避免逐个缺失
_seen = set()
for _base in [sys.prefix, sys.base_prefix]:
    _lib_bin = os.path.join(_base, 'Library', 'bin')
    if not os.path.isdir(_lib_bin):
        continue
    for _dll in _glob.glob(os.path.join(_lib_bin, '*.dll')):
        _name = os.path.basename(_dll).lower()
        if _name not in _seen:
            _seen.add(_name)
            binaries.append((_dll, '.'))

a = Analysis(
    ['上层GUI.py'],
    pathex=[],
    binaries=binaries,
    datas=[
        ('modules', 'modules'),
        *datas,
    ],
    hiddenimports=[
        'pyautogui',
        'PIL',
        'tkinter',
        '_tkinter',
        'ctypes',
        '_ctypes',
        'requests',
        'zhipuai',
        'modules',
        'modules.自动截图模块',
        'modules.自动评分模块',
        'modules.自动填分模块',
        'modules.规则调优模块',
        'modules.评分数据库模块',
        *hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'tkinter.test', 'unittest',
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
    name='自动阅卷系统',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='自动阅卷系统',
)
