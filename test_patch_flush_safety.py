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
from patch_flush_safety import patch_file_atomic, patch_lightrag, patch_utils  # noqa: E402

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

UTILS = '''import re


def fix_tuple_delimiter_corruption(record, delimiter_core, tuple_delimiter):
    if not record or not delimiter_core or not tuple_delimiter:
        return record

    # Escape the delimiter core for regex use
    escaped_delimiter_core = re.escape(delimiter_core)

    record = re.sub(rf"<\\|?{escaped_delimiter_core}\\|?>", tuple_delimiter, record)
    return record
'''

for name, fn, src, marker in (
    ("file_atomic", patch_file_atomic, FILE_ATOMIC, "_fsync_file(tmp, workspace)"),
    ("lightrag", patch_lightrag, LIGHTRAG, "Flush start:"),
    ("utils", patch_utils, UTILS, "ragkit: whitespace inside the delimiter"),
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
    ).replace("re.escape(delimiter_core)", "re.escape(core)")
    moved, status = fn(drifted)
    assert status == "anchor-missing", f"{name}: drifted source returned {status}"
    assert moved == drifted, f"{name}: drifted source was modified anyway"

# the real lightrag.py keeps the tail after _flush_one intact
patched, _ = patch_lightrag(LIGHTRAG)
assert "asyncio.gather" in patched and "return results" in patched, "tail lost"

# the patched fixer repairs whitespace variants and leaves good records alone
ns = {}
exec(patch_utils(UTILS)[0], ns)
fix = ns["fix_tuple_delimiter_corruption"]
D = "<|#|>"
for bad in ("<|# |>", "<| #|>", "<| # |>", "< |#| >"):
    rec = f"entity{D}BaSO4{D}NaturalObject{bad}Sparingly soluble salt"
    assert fix(rec, "#", D).split(D) == ["entity", "BaSO4", "NaturalObject", "Sparingly soluble salt"], bad
good = f"entity{D}a{D}b{D}c"
assert fix(good, "#", D) == good, "good record changed"
assert fix("pH < 7 | # 3 |", "#", D) == "pH < 7 | # 3 |", "plain text changed"

print("OK: patcher is idempotent, syntax-clean, refuses drifted sources, and fixes spaced delimiters")
