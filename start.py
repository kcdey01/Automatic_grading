"""启动脚本：确保虚拟环境可用 → 安装依赖 → 启动上层 GUI。

注意：Windows 下虚拟环境的解释器是 ``venv\\Scripts\\python.exe``，
而 ``os.path.exists`` 不会自动补 ``.exe``，因此判断时必须带上扩展名，
否则会对已存在的 venv 反复重建，进而因文件被占用而报权限错误。
"""

import os
import subprocess
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))

IS_WINDOWS = os.name == "nt"
VENV_DIR = "venv"
VENV_PYTHON = os.path.join(
    VENV_DIR,
    "Scripts" if IS_WINDOWS else "bin",
    "python.exe" if IS_WINDOWS else "python",
)


def ensure_venv():
    """虚拟环境存在则直接复用；仅在确实缺失时才创建。"""
    if os.path.exists(VENV_PYTHON):
        return
    print("[INFO] Creating venv...")
    result = subprocess.run([sys.executable, "-m", "venv", VENV_DIR])
    if result.returncode != 0 or not os.path.exists(VENV_PYTHON):
        print("[ERROR] 创建虚拟环境失败。")
        print("  可能原因：venv 目录已存在且被占用（例如阅卷程序仍在运行）。")
        print("  处理办法：关闭正在使用该环境的程序后重试；仍不行可手动删除 venv 目录后重新运行。")
        sys.exit(1)


def main():
    ensure_venv()

    print("[INFO] Installing dependencies...")
    # 用 `-m pip` 而非 Scripts/pip(.exe)，避免入口脚本缺失/路径问题
    subprocess.run([VENV_PYTHON, "-m", "pip", "install", "-r", "requirements.txt", "-q"])

    print("[INFO] Launching...")
    return subprocess.run([VENV_PYTHON, "上层GUI.py"]).returncode


if __name__ == "__main__":
    sys.exit(main())
