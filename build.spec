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

# 收集 tkinter 的 tcl/tk DLL（Anaconda 环境下 PyInstaller 可能漏掉）
_dll_search_dirs = []
for base in [sys.prefix, sys.base_prefix, os.path.dirname(sys.executable)]:
    _dll_search_dirs.append(os.path.join(base, 'Library', 'bin'))
    _dll_search_dirs.append(os.path.join(base, 'DLLs'))

for _dir in _dll_search_dirs:
    if not os.path.isdir(_dir):
        continue
    for _name in ['tcl86t.dll', 'tk86t.dll', 'tcl8*.dll', 'tk8*.dll']:
        for _f in _glob.glob(os.path.join(_dir, _name)):
            if os.path.isfile(_f):
                binaries.append((_f, '.'))

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
