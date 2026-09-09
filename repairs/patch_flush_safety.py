"""Apply the two flush-phase safety patches to a lightrag package, idempotently.

    python repairs/patch_flush_safety.py [<lightrag package dir> ...]

With no arguments it patches the venv's installed ``lightrag`` package plus, if
RAGBASE_ROOT is set and that base carries a vendored clone, the base's
``lightrag/lightrag``. ingest.ps1 runs it on every ingest, so a pip upgrade or a
fresh base cannot silently drop the patches.

Patch 1 - file_atomic.py: fsync the tmp before ``os.replace``. Without it NTFS
can commit the rename on a bugcheck while the data pages are still cached, and
the destination comes back as a full-length run of NUL bytes (seen on graphml
and vdb_entities after the 2026-08 BSOD).

Patch 2 - lightrag.py: log start/done + seconds + megabytes for every storage
flush. The heavy saves log nothing of their own, so a process killed inside the
flush left a log whose last line named a storage that had already finished -
the 2026-09-08 crash looked like a graphml failure and was not one.

Exit 0 if every target ends up patched (or already was), 1 if an anchor was not
found - upstream moved and the patch needs re-fitting. Never partially writes:
a file is rewritten only when every anchor for it matched.
"""

from __future__ import annotations

import os
import sys

FSYNC_HELPER = '''def _fsync_file(path: str, workspace: str) -> None:
    """Force ``path``'s data to stable storage before the rename.

    ``os.replace`` orders only the *metadata* change. On a bugcheck or power
    loss NTFS can commit the rename while the tmp's data pages are still in
    the cache - the destination then comes back as a full-length run of NUL
    bytes (observed here on graphml and vdb_entities after the 2026-08 BSOD).

    Best-effort: a failed fsync is logged, not raised, so a filesystem that
    cannot flush never blocks the write. O_RDWR because Windows'
    ``_commit`` needs a writable handle.
    """
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        logger.warning(f"[{workspace}] fsync of {path} failed: {exc}")


'''

FLUSH_ONE = '''        def _storage_path(storage_inst):
            for attr in ("_client_file_name", "_graphml_xml_file", "_file_name"):
                path = getattr(storage_inst, attr, None)
                if path:
                    return path
            return None

        async def _flush_one(storage_inst) -> None:
            # Wrap each flush so a failure carries the driver name + namespace.
            # The pipeline uses this to abort the batch with an actionable
            # reason instead of misattributing a shared-buffer flush error to
            # whichever document happened to trigger index_done_callback.
            #
            # The start/done lines are load-bearing for crash forensics: the
            # heavy saves (a multi-GB vector store) log nothing of their own,
            # so a process killed inside the flush left a log whose last line
            # named a *different* storage that had already finished writing.
            name = type(storage_inst).__name__
            namespace = getattr(storage_inst, "final_namespace", None) or getattr(
                storage_inst, "namespace", ""
            )
            logger.info(f"Flush start: {name}[{namespace}]")
            started = time.perf_counter()
            try:
                await cast(StorageNameSpace, storage_inst).index_done_callback()
            except Exception as e:
                raise IndexFlushError(name, namespace, e) from e
            path = _storage_path(storage_inst)
            try:
                size_mb = os.path.getsize(path) / 2**20 if path else 0.0
            except OSError:
                size_mb = 0.0
            logger.info(
                f"Flush done: {name}[{namespace}] "
                f"{time.perf_counter() - started:.1f}s {size_mb:.1f} MB"
            )
'''

_REAP = "def reap_orphan_tmp_files("
_WRITE_FN = "        write_fn(tmp)\n        _preserve_mode("
_FLUSH_START = "        async def _flush_one(storage_inst) -> None:"
_FLUSH_END = ") from e\n"


def patch_file_atomic(src: str) -> tuple[str, str]:
    """Return (new_source, status) for file_atomic.py."""
    if "_fsync_file" in src:
        return src, "already"
    if _REAP not in src or _WRITE_FN not in src:
        return src, "anchor-missing"
    src = src.replace(_REAP, FSYNC_HELPER + _REAP, 1)
    src = src.replace(
        _WRITE_FN,
        "        write_fn(tmp)\n        _fsync_file(tmp, workspace)\n        _preserve_mode(",
        1,
    )
    return src, "patched"


def patch_lightrag(src: str) -> tuple[str, str]:
    """Return (new_source, status) for lightrag.py."""
    if "Flush start:" in src:
        return src, "already"
    start = src.find(_FLUSH_START)
    if start == -1:
        return src, "anchor-missing"
    end = src.find(_FLUSH_END, start)
    if end == -1:
        return src, "anchor-missing"
    end += len(_FLUSH_END)
    return src[:start] + FLUSH_ONE + src[end:], "patched"


TARGETS = (("file_atomic.py", patch_file_atomic), ("lightrag.py", patch_lightrag))


def apply_to_package(pkg_dir: str) -> bool:
    """Patch both files in ``pkg_dir``. True if the package is fully patched."""
    ok = True
    for name, fn in TARGETS:
        path = os.path.join(pkg_dir, name)
        if not os.path.isfile(path):
            print(f"flush-safety: {path}: MISSING")
            ok = False
            continue
        with open(path, encoding="utf-8") as f:
            src = f.read()
        new_src, status = fn(src)
        if status == "patched":
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_src)
        elif status == "anchor-missing":
            ok = False
        print(f"flush-safety: {path}: {status}")
    return ok


def default_targets() -> list[str]:
    import lightrag  # the package the ingest will actually import

    found = [os.path.dirname(os.path.abspath(lightrag.__file__))]
    root = os.environ.get("RAGBASE_ROOT")
    if root:
        vendored = os.path.join(root, "lightrag", "lightrag")
        if os.path.isdir(vendored) and vendored not in found:
            found.append(vendored)
    return found


if __name__ == "__main__":
    targets = sys.argv[1:] or default_targets()
    sys.exit(0 if all([apply_to_package(t) for t in targets]) else 1)
