#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
系统通知模块

发送 Windows 系统通知（任务栏气泡 / 通知中心），用于提醒关键事件：
- API 无响应（阅卷终止）
- 任务完成
- 三轮校验分数均不一致

实现方式：调用系统自带的 PowerShell + WinForms NotifyIcon 发送气泡通知，
不依赖第三方库；发送本身非阻塞（后台进程），失败时静默降级（仅打印日志）。
"""

import base64
import os
import subprocess

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def _ps_quote(text) -> str:
    """把文本转成 PowerShell 单引号字符串字面量（转义单引号，去掉 \\r）。"""
    cleaned = str(text).replace("\r", "")
    return "'" + cleaned.replace("'", "''") + "'"


def send_windows_notification(title: str, message: str, level: str = "info",
                              timeout_ms: int = 10000) -> bool:
    """发送一条 Windows 系统通知（非阻塞，后台进程执行）。

    :param title:      通知标题
    :param message:    通知内容（过长时截断到 220 字符）
    :param level:      "info"（普通）或 "warning"（警告图标）
    :param timeout_ms: 通知显示时长（毫秒，系统可能忽略）
    :return: 是否成功发起发送（不代表用户已看到通知）
    """
    if os.name != "nt":
        return False
    try:
        is_warning = str(level).lower() == "warning"
        icon_expr = "[System.Drawing.SystemIcons]::Warning" if is_warning else "[System.Drawing.SystemIcons]::Information"
        tip_icon = "Warning" if is_warning else "Info"

        balloon_text = str(message)
        if len(balloon_text) > 220:
            balloon_text = balloon_text[:220] + "…"

        script = (
            "$ErrorActionPreference = 'SilentlyContinue'\n"
            "Add-Type -AssemblyName System.Windows.Forms\n"
            "Add-Type -AssemblyName System.Drawing\n"
            "$n = New-Object System.Windows.Forms.NotifyIcon\n"
            f"$n.Icon = {icon_expr}\n"
            f"$n.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::{tip_icon}\n"
            f"$n.BalloonTipTitle = {_ps_quote(title)}\n"
            f"$n.BalloonTipText = {_ps_quote(balloon_text)}\n"
            "$n.Visible = $true\n"
            f"$n.ShowBalloonTip({int(timeout_ms)})\n"
            "Start-Sleep -Milliseconds 6000\n"
            "$n.Visible = $false\n"
            "$n.Dispose()\n"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-ExecutionPolicy", "Bypass",
                "-EncodedCommand", encoded,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW,
        )
        return True
    except Exception as e:
        try:
            print(f"[系统通知] 发送失败：{e}")
        except Exception:
            pass
        return False
