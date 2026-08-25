"""Classify why a rag_ingest.py run failed. Usage: ingest_triage.py <failed_log> [pdf ...]"""
import glob
import os
import re
import subprocess
import sys

import ragbase

MINERU_OUT = os.path.join(ragbase.DATA, "mineru_output")

RULES = [
    (r"MineruExecutionError|Mineru command failed", "MINERU_PARSE_FAILED",
     "MinerU died during parse. Most common cause here: doc too big for 5GB VRAM / 32GB RAM. "
     "Slice the PDF into <=10-page parts and re-run."),
    (r"CUDA out of memory|CUBLAS_STATUS_ALLOC_FAILED|cudaErrorMemoryAllocation", "CUDA_OOM",
     "Slice the PDF, or set MINERU_DEVICE_MODE='cpu' in ingest_resume.ps1."),
    (r"MemoryError|paging file is too small|Cannot allocate memory", "HOST_OOM",
     "Host RAM exhausted. Slice the PDF."),
    (r"429|rate limit|1305", "LLM_RATE_LIMIT",
     "z.ai concurrency limit. Lower the VLM semaphore in rag_ingest.py and re-run; the run is resumable."),
    (r"ConnectionError|Connection refused|Max retries exceeded|Failed to establish", "ENDPOINT_UNREACHABLE",
     "Ollama or the LLM endpoint was down. Start it and re-run."),
    (r"KeyboardInterrupt", "INTERRUPTED", "Run was interrupted; just re-run."),
]


def classify(text):
    for pat, verdict, hint in RULES:
        if re.search(pat, text, re.I):
            return verdict, hint
    return "UNKNOWN", "See TAIL below and the full log."


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15).stdout.strip()


def pdf_info(path):
    try:
        import fitz
        doc = fitz.open(path)
        imgs = draws = chars = 0
        maxdraw = 0
        for page in doc:
            imgs += len(page.get_images())
            d = len(page.get_drawings())
            maxdraw = max(maxdraw, d)
            chars += len(page.get_text())
        return "pages=%d images=%d maxdraw=%d chars=%d" % (doc.page_count, imgs, maxdraw, chars)
    except Exception:
        try:
            return "pages=n/a images=n/a maxdraw=n/a chars=n/a size=%dMB" % (os.path.getsize(path) >> 20)
        except Exception:
            return "pages=n/a images=n/a maxdraw=n/a chars=n/a"


def selftest():
    assert classify("raise MineruExecutionError(...)")[0] == "MINERU_PARSE_FAILED"
    assert classify("torch.cuda: CUDA out of memory. Tried to allocate")[0] == "CUDA_OOM"
    assert classify("HTTP 429 Too Many Requests")[0] == "LLM_RATE_LIMIT"
    assert classify("Max retries exceeded with url: /api/generate")[0] == "ENDPOINT_UNREACHABLE"
    assert classify("everything was fine actually")[0] == "UNKNOWN"
    print("SELFTEST OK")


if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
    selftest()
    sys.exit(0)

log = sys.argv[1]
text = open(log, "rb").read().decode("utf-8", "replace").replace("\x00", "")
lines = [ln.rstrip() for ln in text.splitlines()]
verdict, hint = classify(text)

progress = "n/a"
try:
    hits = [ln for ln in lines if ln.strip() and re.search(r"Predict:.*\|", ln)]
    progress = hits[-1].strip() if hits else "n/a"
except Exception:
    pass

try:
    gpu = run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
               "--format=csv,noheader"]) or "n/a"
except Exception:
    gpu = "n/a"

try:
    import psutil
    ram = "%.1f GB total" % (psutil.virtual_memory().total / 2**30)
except Exception:
    try:
        ram = " ".join(run(["wmic", "computersystem", "get", "TotalPhysicalMemory"]).split()[1:]) or "n/a"
    except Exception:
        ram = "n/a"

print("VERDICT: " + verdict)
print("HINT: " + hint)
print("PROGRESS: " + progress)
print("GPU: " + gpu)
print("RAM: " + ram)
for p in sys.argv[2:]:
    stem = os.path.splitext(os.path.basename(p))[0]
    try:
        found = glob.glob(os.path.join(MINERU_OUT, stem + "*", "**", "*_content_list.json"), recursive=True)
        out = "present" if found else "EMPTY (nothing parsed)"
    except Exception:
        out = "n/a"
    print("INPUT: %s %s output: %s" % (os.path.basename(p), pdf_info(p), out))
print("TAIL:")
print("\n".join(lines[-30:]))
