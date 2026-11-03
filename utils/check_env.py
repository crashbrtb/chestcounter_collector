"""
Environment diagnostics for Total Battle Chest Collector.
Checks Python, OpenCV, and dependencies.
"""

import os
import sys

# Add project root to sys.path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def check():
    print("=" * 70)
    print("      ENVIRONMENT DIAGNOSTICS - TOTAL BATTLE CHEST COLLECTOR")
    print("=" * 70)

    # 1. Python
    print("\n[1] Python:")
    print(f"    Version:    {sys.version.split()[0]} ({'64-bit' if sys.maxsize > 2**32 else '32-bit'})")
    print(f"    Executable: {sys.executable}")
    is_venv = hasattr(sys, "real_prefix") or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    print(f"    Virtual Environment (venv): {'YES' if is_venv else 'NO (using system Python)'}")
    if not is_venv:
        print("    [!] WARNING: It is strongly recommended to use the project venv (run install.bat).")

    # 2. Libraries
    print("\n[2] Libraries:")
    missing = []
    for mod in ["cv2", "PIL", "customtkinter", "mysql.connector", "websocket", "requests"]:
        try:
            __import__(mod)
            print(f"    {mod:<18}: [OK]")
        except ImportError as exc:
            missing.append(mod)
            print(f"    {mod:<18}: [FAILED - {exc}]")

    print("\n" + "=" * 70)
    if missing:
        print(f"  RESULT: Missing {', '.join(missing)}.")
        print("          Fix: Run 'install.bat' on this computer.")
    else:
        print("  RESULT: Environment is READY!")
    print("=" * 70)


if __name__ == "__main__":
    check()
