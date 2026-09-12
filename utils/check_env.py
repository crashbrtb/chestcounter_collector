"""
Diagnóstico de ambiente do Total Battle Chest Collector.
Verifica Python, RapidOCR, ONNXRuntime, Tesseract, OpenCV e dependências.
"""

import sys
import os

# Adiciona raiz do projeto ao sys.path
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
    print("      DIAGNÓSTICO DE AMBIENTE - TOTAL BATTLE CHEST COLLECTOR")
    print("=" * 70)

    # 1. Python
    print(f"\n[1] Python:")
    print(f"    Versão:    {sys.version.split()[0]} ({'64-bit' if sys.maxsize > 2**32 else '32-bit'})")
    print(f"    Executável: {sys.executable}")
    is_venv = hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix)
    print(f"    Ambiente Virtual (venv): {'SIM' if is_venv else 'NÃO (usando Python global do sistema)'}")
    if not is_venv:
        print("    [!] AVISO: É altamente recomendado usar o ambiente virtual do projeto (execute install.bat).")

    # 2. RapidOCR & ONNXRuntime
    print(f"\n[2] RapidOCR & ONNXRuntime (Motor Principal):")
    rapid_ok = False
    try:
        from core.ocr import RapidOCRBackend, HAS_RAPIDOCR, RAPIDOCR_API, RAPIDOCR_IMPORT_ERROR
        if HAS_RAPIDOCR:
            backend = RapidOCRBackend()
            available, msg = backend.available()
            if available:
                print(f"    Status: [OK] ATIVO ({msg})")
                print("    Testando carregamento do modelo ONNX...")
                try:
                    engine = backend._get_engine()
                    print("    Carregamento do modelo: [OK] Modelos carregados com sucesso!")
                    rapid_ok = True
                except Exception as exc:
                    print(f"    Carregamento do modelo: [FALHA] {exc}")
            else:
                print(f"    Status: [FALHA] {msg}")
        else:
            print(f"    Status: [FALHA] RapidOCR não pôde ser importado.")
            if RAPIDOCR_IMPORT_ERROR:
                print(f"    Detalhes do erro: {RAPIDOCR_IMPORT_ERROR}")
                if "DLL load failed" in RAPIDOCR_IMPORT_ERROR or "onnxruntime" in RAPIDOCR_IMPORT_ERROR:
                    print("\n    >>> CAUSA MAIS COMUM:")
                    print("    Falta o 'Microsoft Visual C++ Redistributable 2015-2022 (x64)'.")
                    print("    Baixe e instale: https://aka.ms/vs/17/release/vc_redist.x64.exe")
    except Exception as exc:
        print(f"    Erro ao verificar RapidOCR: {exc}")

    if not rapid_ok:
        print("\n    [!!!] ATENÇÃO: Sem o RapidOCR, o contador NÃO consegue identificar")
        print("          os perfis na lista e não consegue ler nomes de jogadores.")
        print("          Execute 'install.bat' para instalar as dependências.")

    # 3. Tesseract
    print(f"\n[3] Tesseract (Motor Secundário / Fallback):")
    try:
        from core.ocr import TesseractBackend, HAS_TESSERACT
        if HAS_TESSERACT:
            tess = TesseractBackend()
            ok, msg = tess.available()
            if ok:
                print(f"    Status: [OK] Encontrado ({msg})")
            else:
                print(f"    Status: [AVISO] {msg}")
        else:
            print("    Status: [NÃO INSTALADO] Pacote 'pytesseract' não encontrado.")
    except Exception as exc:
        print(f"    Erro ao verificar Tesseract: {exc}")

    # 4. Outras dependências críticas
    print(f"\n[4] Bibliotecas Auxiliares:")
    for mod in ["cv2", "PIL", "customtkinter", "mysql.connector", "websocket", "requests"]:
        try:
            __import__(mod)
            print(f"    {mod:<18}: [OK]")
        except ImportError as exc:
            print(f"    {mod:<18}: [FALHA - {exc}]")

    print("\n" + "=" * 70)
    if rapid_ok:
        print("  RESULTADO: O ambiente está PRONTO! O RapidOCR está ativo.")
    else:
        print("  RESULTADO: O RapidOCR NÃO está funcionando neste computador.")
        print("             Solução: Execute 'install.bat' neste computador.")
    print("=" * 70)

if __name__ == "__main__":
    check()
