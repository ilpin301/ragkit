"""Check repairs/patch_flush_safety.py on synthetic sources.

    python test_patch_flush_safety.py

Guards the patcher that ingest.ps1 runs against every lightrag package before
every ingest: it must be idempotent (a second run must not double-insert), it
must produce syntactically valid Python, and it must REFUSE (anchor-missing,
non-zero exit) instead of half-patching when upstream moves the code.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "repairs"))
from patch_flush_safety import patch_file_atomic, patch_lightrag  # noqa: E402

FILE_ATOMIC = '''import os
logger = None


def tmp_path_for(file_name):
    return file_name + ".tmp"


def reap_orphan_tmp_files(file_name, workspace="_"):
    pass


def atomic_write(file_name, write_fn, workspace="_"):
    tmp = tmp_path_for(file_name)
    try:
        write_fn(tmp)
        _preserve_mode(tmp, file_name, workspace)
        os.replace(tmp, file_name)
    except BaseException:
        raise
'''

LIGHTRAG = '''class LightRAG:
    async def _flush_storages(self, storages: list) -> None:
        """Doc."""

        async def _flush_one(storage_inst) -> None:
            # Wrap each flush so a failure carries the driver name + namespace.
            try:
                await cast(StorageNameSpace, storage_inst).index_done_callback()
            except Exception as e:
                namespace = getattr(storage_inst, "final_namespace", None) or getattr(
                    storage_inst, "namespace", ""
                )
                raise IndexFlushError(type(storage_inst).__name__, namespace, e) from e

        results = await asyncio.gather(*[_flush_one(i) for i in storages])
        return results
'''

for name, fn, src, marker in (
    ("file_atomic", patch_file_atomic, FILE_ATOMIC, "_fsync_file(tmp, workspace)"),
    ("lightrag", patch_lightrag, LIGHTRAG, "Flush start:"),
):
    once, status = fn(src)
    assert status == "patched", f"{name}: first pass returned {status}"
    assert marker in once, f"{name}: patch marker missing"
    compile(once, f"<{name}>", "exec")

    twice, status = fn(once)
    assert status == "already", f"{name}: second pass returned {status}"
    assert twice == once, f"{name}: second pass changed the file"
    assert once.count(marker) == 1, f"{name}: marker inserted twice"

    drifted = src.replace("write_fn(tmp)", "write_fn(TMP)").replace(
        "async def _flush_one(storage_inst) -> None:", "async def _flush_one(inst):"
    )
    moved, status = fn(drifted)
    assert status == "anchor-missing", f"{name}: drifted source returned {status}"
    assert moved == drifted, f"{name}: drifted source was modified anyway"

# the real lightrag.py keeps the tail after _flush_one intact
patched, _ = patch_lightrag(LIGHTRAG)
assert "asyncio.gather" in patched and "return results" in patched, "tail lost"

print("OK: patcher is idempotent, syntax-clean, and refuses drifted sources")
