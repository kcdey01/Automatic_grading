# -*- mode: python ; coding: utf-8 -*-
import os
import sys

block_cipher = None

a = Analysis(
    ['上层GUI.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('modules', 'modules'),
    ],
    hiddenimports=[
        'pyautogui',
        'PIL',
        'PIL._tkinter_finder',
        'requests',
        'zhipuai',
        'modules',
        'modules.自动截图模块',
        'modules.自动评分模块',
        'modules.自动填分模块',
        'modules.规则调优模块',
        'modules.评分数据库模块',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
