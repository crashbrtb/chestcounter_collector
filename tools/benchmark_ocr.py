"""
Measures RapidOCR on this machine, CPU against GPU (DirectML), before `ocr.use_gpu`
is turned on for a real run.

Why this exists rather than a fixed number in the docs: DirectML's payoff depends
on the GPU actually in the machine, its driver, and which OCR models are loaded -
on an RTX 4050 with the current PP-OCRv6 models this measured 403 ms per panel on
4 CPU threads against 103 ms on the GPU, but an older or integrated GPU can easily
come out slower than the CPU for work this small. Assuming the ratio carries over
is how a run ends up quietly slower after 'enabling the GPU'; this script measures
the machine it runs on instead.

Usage:
    python tools/benchmark_ocr.py                  # synthetic panel, quick check
    python tools/benchmark_ocr.py --image path.png # a real saved chest panel
"""

import argparse
import logging
import os
import sys
import time

import numpy as np

logging.getLogger("RapidOCR").setLevel(logging.WARNING)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import cv2

from core.ocr import HAS_RAPIDOCR, RAPIDOCR_API, RAPIDOCR_IMPORT_ERROR, RapidOCRBackend


def synthetic_panel() -> np.ndarray:
    """
    A stand-in for a real chest panel: dark background, four blocks of three
    lines each, the shapes the real thing produces (a title, then two
    'Label: value' rows) without needing a saved screenshot to run this.

    Timing on this image is a rough proxy, not a promise - real panels carry a
    textured background and accented names that cost the recognition step more
    than plain Latin text does. It is enough to tell whether the GPU is even in
    the running on this machine; `--image` is what settles the real number.
    """
    height, width = 620, 700
    image = np.full((height, width, 3), 40, dtype=np.uint8)
    rows = [
        "Epic Monster Chest", "From: Kenan Baskan", "Source: Epic Crypt",
        "Legendary Chest", "From: Aklin Sasar", "Source: Level 35",
        "Golden Chest T6000SK", "From: Gugenot", "Source: Squad Raid",
        "Silver Chest", "From: Vali", "Source: Tolga46",
    ]
    y = 40
    step = height // len(rows)
    for text in rows:
        cv2.putText(image, text, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8,
                    (225, 225, 225), 1, cv2.LINE_AA)
        y += step
    return image


def timed_reads(backend: RapidOCRBackend, image: np.ndarray, warmup: int, iters: int):
    for _ in range(warmup):
        backend.read(image)
    times = []
    for _ in range(iters):
        start = time.perf_counter()
        backend.read(image)
        times.append((time.perf_counter() - start) * 1000)
    return times


def summarize(label: str, times):
    avg = sum(times) / len(times)
    print(f"  {label:<22} avg {avg:7.0f} ms   " +
          "  ".join(f"{t:.0f}" for t in times))
    return avg


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", help="A saved chest panel (PNG) instead of the synthetic one.")
    parser.add_argument("--threads", type=int, default=4, help="CPU threads to compare against (default 4).")
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--iters", type=int, default=8)
    args = parser.parse_args()

    if not HAS_RAPIDOCR:
        print(f"RapidOCR is not installed or failed to load: {RAPIDOCR_IMPORT_ERROR}")
        print("Run install.bat first.")
        return 1

    print(f"RapidOCR package in use: {RAPIDOCR_API or 'unknown'}")
    if RAPIDOCR_API == "legacy":
        print("This is the OLD 'rapidocr-onnxruntime' package (capped at 1.4.4, older "
              "PP-OCRv4 models). requirements.txt asks for 'rapidocr>=3.0.0' - if that "
              "is not installed, 'pip install rapidocr' (after 'pip uninstall "
              "rapidocr-onnxruntime') gets the current models this project was tuned "
              "against, before benchmarking further.")

    try:
        import onnxruntime as ort
        providers = ort.get_available_providers()
    except Exception as exc:
        providers = []
        print(f"Could not inspect onnxruntime providers: {exc}")
    print(f"onnxruntime providers available: {providers}")
    if "DmlExecutionProvider" not in providers:
        print("DirectML is NOT available - the GPU pass below will silently run on the "
              "CPU instead (RapidOCR falls back and logs a warning). To test the GPU:")
        print("    pip uninstall onnxruntime onnxruntime-gpu")
        print("    pip install onnxruntime-directml")
        print("(the two cannot be installed side by side - installing one replaces the other)")

    image = cv2.imread(args.image) if args.image else synthetic_panel()
    if image is None:
        print(f"Could not read image: {args.image}")
        return 1
    print(f"\nPanel: {'synthetic' if not args.image else args.image} ({image.shape[1]}x{image.shape[0]})")
    print(f"Warmup {args.warmup}, measured iterations {args.iters}\n")

    cpu_backend = RapidOCRBackend(threads=args.threads, use_gpu=False)
    cpu_times = timed_reads(cpu_backend, image, args.warmup, args.iters)
    cpu_avg = summarize(f"CPU ({args.threads} threads)", cpu_times)

    gpu_backend = RapidOCRBackend(threads=args.threads, use_gpu=True)
    gpu_times = timed_reads(gpu_backend, image, args.warmup, args.iters)
    gpu_avg = summarize("GPU (DirectML)", gpu_times)

    print()
    if gpu_avg < cpu_avg * 0.9:
        print(f"GPU is faster here: {cpu_avg / gpu_avg:.1f}x. Worth turning on 'ocr.use_gpu'.")
    elif gpu_avg > cpu_avg * 1.1:
        print(f"GPU is SLOWER here ({gpu_avg / cpu_avg:.1f}x): leave 'ocr.use_gpu' off on this machine.")
    else:
        print("No meaningful difference on this machine either way.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
