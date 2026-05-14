import subprocess, sys, os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

venv_python = os.path.join("venv", "Scripts" if os.name == "nt" else "bin", "python")

if not os.path.exists(venv_python):
    print("[INFO] Creating venv...")
    subprocess.run([sys.executable, "-m", "venv", "venv"], check=True)

pip = os.path.join(os.path.dirname(venv_python), "pip")
print("[INFO] Installing dependencies...")
subprocess.run([pip, "install", "-r", "requirements.txt", "-q"], check=False)

print("[INFO] Launching...")
subprocess.run([venv_python, "\u4e0a\u5c42GUI.py"])