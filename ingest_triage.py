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
    # Bare digits used to be enough here (r"429|rate limit|1305"), so "1305" and "429"
    # matched chunk ids, byte offsets and token counts anywhere in a 700 KB log and every
    # failure came back LLM_RATE_LIMIT. Each alternative now needs its own context.
    (r"HTTP.{0,8}429\b|\b429 Too Many|\b429 Client Error|code.{0,6}429\b"
     r"|rate.?limit|code.{0,6}1305\b", "LLM_RATE_LIMIT",
     "z.ai concurrency limit. Lower the VLM semaphore in rag_ingest.py and re-run; the run is resumable."),
    (r"ConnectionError|Connection refused|Max retries exceeded|Failed to establish", "ENDPOINT_UNREACHABLE",
     "Ollama or the LLM endpoint was down. Start it and re-run."),
    (r"KeyboardInterrupt", "INTERRUPTED", "Run was interrupted; just re-run."),
]


# Negative exit codes are NTSTATUS: the OS killed the interpreter. Nothing in the log text
# can outrank that, because the log is only what the run managed to print before it died.
NATIVE_CRASH = {
    -1073741819: ("PROCESS_ACCESS_VIOLATION",
                  "0xC0000005. The OS killed the process; this was not an API error. Open the WER "
                  "dump (LocalDumps DumpFolder) in cdb and run !analyze -v. A HEAP_CORRUPTION "
                  "bucket points at memory hardware, not at a code bug."),
    -1073740940: ("PROCESS_HEAP_CORRUPTION",
                  "0xC0000374. The allocator found its own metadata corrupt. Suspect RAM before any "
                  "library; confirm in the WER dump."),
    -1073741571: ("PROCESS_STACK_OVERFLOW", "0xC00000FD. Runaway recursion; check the dump."),
    -1073741795: ("PROCESS_ILLEGAL_INSTRUCTION",
                  "0xC000001D. Bad opcode - corrupt memory, or a binary built for another CPU."),
    -1073741510: ("PROCESS_CTRL_C", "0xC000013A. Someone stopped the run; just re-run."),
}


def classify_exit(code):
    """Verdict from the process exit code, or None when the code carries no signal."""
    if code is None:
        return None
    if code in NATIVE_CRASH:
        return NATIVE_CRASH[code]
    if code < 0:
        return ("PROCESS_CRASHED_0x%08X" % (code & 0xFFFFFFFF),
                "The OS killed the process. Open the WER dump in cdb and run !analyze -v.")
    return None


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
    # the regression this fix exists for: bare digits matched ids and offsets
    assert classify("chunk-1305abc at offset 4291, 1305 tokens")[0] == "UNKNOWN"
    assert classify('{"code": "1305", "message": "concurrency"}')[0] == "LLM_RATE_LIMIT"
    assert classify("Error code: 429")[0] == "LLM_RATE_LIMIT"
    assert classify_exit(-1073741819)[0] == "PROCESS_ACCESS_VIOLATION"
    assert classify_exit(-1073740940)[0] == "PROCESS_HEAP_CORRUPTION"
    assert classify_exit(1) is None and classify_exit(None) is None
    assert classify_exit(-559038737)[0].startswith("PROCESS_CRASHED_0x")
    # a native crash must outrank any text rule
    assert (classify_exit(-1073741819) or classify("HTTP 429 Too Many Requests"))[0] \
        == "PROCESS_ACCESS_VIOLATION"
    print("SELFTEST OK")


if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
    selftest()
    sys.exit(0)

argv = sys.argv[1:]
exitcode = None
if "--exitcode" in argv:
    i = argv.index("--exitcode")
    try:
        exitcode = int(argv[i + 1])
    except (IndexError, ValueError):
        exitcode = None
    del argv[i:i + 2]

log = argv[0]
text = open(log, "rb").read().decode("utf-8", "replace").replace("\x00", "")
lines = [ln.rstrip() for ln in text.splitlines()]
verdict, hint = classify_exit(exitcode) or classify(text)

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
for p in argv[1:]:
    stem = os.path.splitext(os.path.basename(p))[0]
    try:
        found = glob.glob(os.path.join(MINERU_OUT, stem + "*", "**", "*_content_list.json"), recursive=True)
        out = "present" if found else "EMPTY (nothing parsed)"
    except Exception:
        out = "n/a"
    print("INPUT: %s %s output: %s" % (os.path.basename(p), pdf_info(p), out))
print("TAIL:")
print("\n".join(lines[-30:]))
