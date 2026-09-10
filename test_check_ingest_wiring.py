"""Self-check for check_ingest_wiring.py -- no server writes, no LLM calls.

The point of check_ingest_wiring.py is to catch two silent-wiring
regressions: a stray RAGAnything (...) call outside build_rag/parse_one,
and build_rag failing to forward vector_storage into lightrag_kwargs. This
proves the checker actually catches both, by mutating a COPY of the kit and
running the real checker against it as a subprocess.

    RAGBASE_ROOT=<any base with a valid lightrag/.env> python test_check_ingest_wiring.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = r"X:\RAG_MAIN\RAG\.venv\Scripts\python.exe"
FILES = (
    "rag_ingest.py",
    "ingest_merged.py",
    "check_ingest_wiring.py",
    "ragbase.py",
    "migrate_nano_to_qdrant.py",
)
RUN_ENV = dict(os.environ)
RUN_ENV.update(
    NO_PROXY="*",
    RAGBASE_ROOT="X:/RAG_MAIN/PCM_RAG",
    PYTHONIOENCODING="utf-8",
)


def _make_copy():
    d = tempfile.mkdtemp(prefix="check_ingest_wiring_test_")
    for fname in FILES:
        shutil.copy(os.path.join(HERE, fname), os.path.join(d, fname))
    return d


def _run(d):
    proc = subprocess.run(
        [PY, "check_ingest_wiring.py"],
        cwd=d,
        env=RUN_ENV,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_control_unmodified_kit_passes():
    d = _make_copy()
    try:
        code, out = _run(d)
        assert code == 0, "unmodified kit did not pass (exit %d):\n%s" % (code, out)
        assert "INGEST WIRING CHECK OK" in out, out
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok  control: unmodified copy exits 0")


def test_stray_raganything_detected():
    """Regression A: a RAGAnything (...) construction outside build_rag/parse_one."""
    d = _make_copy()
    try:
        path = os.path.join(d, "ingest_merged.py")
        src = open(path, encoding="utf-8").read()
        # Inject a bogus top-level call well outside parse_one's body.
        mutated = src + "\n\n_stray = " + "RAGAn" + "ything(bogus=True)\n"
        open(path, "w", encoding="utf-8").write(mutated)

        code, out = _run(d)
        assert code == 3, "stray RAGAnything (...) was NOT detected (exit %d):\n%s" % (code, out)
        assert "ingest_merged.py" in out, out
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok  regression A: stray RAGAnything (...) outside build_rag/parse_one -> exit 3")


def test_dropped_vector_storage_forwarding_detected():
    """Regression B: build_rag no longer forwards vector_storage."""
    d = _make_copy()
    try:
        path = os.path.join(d, "rag_ingest.py")
        src = open(path, encoding="utf-8").read()
        needle = 'lightrag_kwargs.setdefault("vector_storage", vector_storage)'
        assert needle in src, "fixture assumption broken -- rag_ingest.py changed shape"
        mutated = src.replace(needle, "pass  # vector_storage forwarding silently dropped")
        assert mutated != src
        open(path, "w", encoding="utf-8").write(mutated)

        code, out = _run(d)
        assert code == 3, "dropped vector_storage forwarding was NOT detected (exit %d):\n%s" % (code, out)
        assert "vector_storage" in out, out
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok  regression B: build_rag dropping vector_storage forwarding -> exit 3")


def main():
    test_control_unmodified_kit_passes()
    test_stray_raganything_detected()
    test_dropped_vector_storage_forwarding_detected()
    print("\nOK: check_ingest_wiring self-check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
