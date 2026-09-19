"""
Environment diagnostics for Total Battle Chest Collector.
Checks Python, RapidOCR, ONNXRuntime, Tesseract, OpenCV, and dependencies.
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

    # 2. RapidOCR & ONNXRuntime
    print("\n[2] RapidOCR & ONNXRuntime (Primary OCR Engine):")
    rapid_ok = False
    try:
        from core.ocr import HAS_RAPIDOCR, RAPIDOCR_API, RAPIDOCR_IMPORT_ERROR, RapidOCRBackend

        if HAS_RAPIDOCR:
            if RAPIDOCR_API == "legacy":
                print("    Package: 'rapidocr-onnxruntime' (OLD - capped at 1.4.4, older PP-OCRv4 "
                      "models).")
                print("             requirements.txt asks for 'rapidocr>=3.0.0' (current PP-OCRv6 "
                      "models). Fix: pip uninstall rapidocr-onnxruntime && pip install rapidocr")
            else:
                print("    Package: 'rapidocr' (current)")
            backend = RapidOCRBackend()
            available, msg = backend.available()
            if available:
                print(f"    Status: [OK] ACTIVE ({msg})")
                print("    Testing ONNX model loading...")
                try:
                    _ = backend._get_engine()
                    print("    Model loading: [OK] Models loaded successfully!")
                    rapid_ok = True
                except Exception as exc:
                    print(f"    Model loading: [FAILED] {exc}")
            else:
                print(f"    Status: [FAILED] {msg}")
        else:
            print("    Status: [FAILED] RapidOCR could not be imported.")
            if RAPIDOCR_IMPORT_ERROR:
                print(f"    Error details: {RAPIDOCR_IMPORT_ERROR}")
                if "DLL load failed" in RAPIDOCR_IMPORT_ERROR or "onnxruntime" in RAPIDOCR_IMPORT_ERROR:
                    print("\n    >>> MOST COMMON CAUSE:")
                    print("    Missing 'Microsoft Visual C++ Redistributable 2015-2022 (x64)'.")
                    print("    Download and install: https://aka.ms/vs/17/release/vc_redist.x64.exe")
    except Exception as exc:
        print(f"    Error checking RapidOCR: {exc}")

    if not rapid_ok:
        print("\n    [!!!] WARNING: Without RapidOCR, the collector CANNOT identify")
        print("          profiles in the list and cannot reliably read player names.")
        print("          Run 'install.bat' to install required dependencies.")

    # 2b. GPU acceleration (DirectML)
    print("\n[2b] GPU acceleration (ocr.use_gpu):")
    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
        print(f"    onnxruntime providers: {providers}")
        if "DmlExecutionProvider" in providers:
            print("    Status: [OK] DirectML available - 'ocr.use_gpu' can use the GPU.")
        else:
            print("    Status: [OFF] DirectML not available - 'ocr.use_gpu' would silently read")
            print("           on the CPU instead. To enable: pip uninstall onnxruntime && "
                  "pip install onnxruntime-directml")
        print("    Run 'python tools/benchmark_ocr.py' to measure CPU vs GPU on this machine "
              "before turning the setting on - the GPU is not always faster for a model this small.")
    except Exception as exc:
        print(f"    Error checking onnxruntime providers: {exc}")

    # 3. Tesseract
    print("\n[3] Tesseract (Secondary / Fallback Engine):")
    try:
        from core.ocr import HAS_TESSERACT, TesseractBackend

        if HAS_TESSERACT:
            tess = TesseractBackend()
            ok, msg = tess.available()
            if ok:
                print(f"    Status: [OK] Found ({msg})")
            else:
                print(f"    Status: [WARNING] {msg}")
        else:
            print("    Status: [NOT INSTALLED] 'pytesseract' package not found.")
    except Exception as exc:
        print(f"    Error checking Tesseract: {exc}")

    # 4. Auxiliary libraries
    print("\n[4] Auxiliary Libraries:")
    for mod in ["cv2", "PIL", "customtkinter", "mysql.connector", "websocket", "requests"]:
        try:
            __import__(mod)
            print(f"    {mod:<18}: [OK]")
        except ImportError as exc:
            print(f"    {mod:<18}: [FAILED - {exc}]")

    print("\n" + "=" * 70)
    if rapid_ok:
        print("  RESULT: Environment is READY! RapidOCR is active.")
    else:
        print("  RESULT: RapidOCR is NOT working on this machine.")
        print("          Fix: Run 'install.bat' on this computer.")
    print("=" * 70)


if __name__ == "__main__":
    check()
