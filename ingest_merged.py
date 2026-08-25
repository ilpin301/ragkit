"""Parse a large PDF in slices, insert it as ONE LightRAG document.

Why this exists: MinerU's page/size ceiling on this box (Quadro P2000, 5GB) is a
PARSING limit, not an ingest limit. `rag_ingest.py` uses
`process_document_complete()`, which fuses parse+insert, so one file becomes one
document -- and slicing to satisfy the parser leaks into the document model.
Chunk overlap never crosses a LightRAG document boundary, so every slice edge
became a hard information break (a sentence hyphenated across a page break ended
up in two different documents, unrecoverable).

Here the two halves are driven separately:
  parse_document()      -> content_list, per slice, in an isolated subprocess
  insert_content_list() -> once, for the merged list, as a single document

Result: one document, zero seams, zero duplicate pages.

GPU safety (this box has BSOD'd on ingest before):
  * one slice per subprocess, so the CUDA context and MinerU weights are fully
    released between slices instead of accumulating
  * oversized embedded images are downscaled before parse (the VLM downscales
    them anyway; MinerU is what chokes holding a giant raster through layout+OCR)
  * every parsed slice is cached to disk, so a crash costs one slice, not the run
  * a slice that fails on cuda is retried once on cpu

Usage:
    python ingest_merged.py <source.pdf> [--pages 10] [--max-image-mb 4]
    python ingest_merged.py --self-test
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import ragbase

HERE = Path(__file__).resolve().parent          # the kit
BASE_DATA = Path(ragbase.DATA)                   # the target base
CACHE = BASE_DATA / "merged_ingest"

# ---------------------------------------------------------------------------
# Text furniture helpers.
# Vendored (not imported) from the il-llm-wiki-pdf-extract-local skill's
# extract.py -- that skill lives outside this repo under %USERPROFILE%\.claude,
# and an ingest path should not break if a user skill dir moves.
# ---------------------------------------------------------------------------

_TRAILING_PAGENUM = re.compile(r"[\s.,;:/\u2013\u2014-]*\d+\s*$")


def join_dehyphenated(lines):
    """Join wrapped lines into one paragraph, fixing end-of-line hyphenation."""
    out = ""
    for raw in lines:
        ln = raw.rstrip()
        if not out:
            out = ln
        elif out.endswith("-"):
            out = out[:-1] + ln.lstrip()
        else:
            out = out + " " + ln.lstrip()
    return out


def _furniture_key(line):
    """Normalize a line for furniture detection: blank out digits and collapse
    whitespace, so running heads that differ only by page number share a key."""
    s = line.strip()
    s = _TRAILING_PAGENUM.sub("", s)
    # Journal running heads put the page number in the MIDDLE
    # ("Materials 2026, 19, 1888  6 of 19"), so stripping only a trailing number
    # leaves a different key per page and the header is never detected. Collapse
    # every digit run instead.
    s = re.sub(r"\d+", "#", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_page_number(line):
    """True for a bare, page-number-like line (1-4 digits only)."""
    return re.fullmatch(r"\d{1,4}", line.strip()) is not None


_SENTENCE_END = re.compile(r"[.!?:;]\s*$")


def _furniture_candidates(lines):
    """Lines eligible to be furniture: the first and last non-empty line of a page,
    excluding anything that reads like a sentence."""
    ne = [ln for ln in lines if ln.strip()]
    if not ne:
        return []
    picks = sorted({0, len(ne) - 1})
    return [ne[i] for i in picks if not _SENTENCE_END.search(ne[i])]


def find_running_lines(pages_text, min_ratio=0.6):
    """Normalized line-forms recurring as page furniture (running heads/footers).

    Only the first and last line of each page are eligible, and only if they do not
    read like a sentence. Without that guard, collapsing digits in `_furniture_key`
    makes ordinary body lines that differ only by a number (e.g. "see Figure 3")
    look identical across pages, and stripping those would be real information loss.
    """
    n = len(pages_text)
    if n == 0:
        return set()
    counts = Counter()
    for lines in pages_text:
        keys = {_furniture_key(ln) for ln in _furniture_candidates(lines)}
        counts.update(k for k in keys if k)
    need = max(2, int(n * min_ratio))
    return {k for k, c in counts.items() if c >= need}


# ---------------------------------------------------------------------------
# Slicing + image shrinking
# ---------------------------------------------------------------------------

JUNK_TYPES = {"page_number", "header", "footer"}


def slice_pdf(src, out_dir, pages_per_slice):
    """Cut src into non-overlapping slices. Returns [(path, first_page_idx0)]."""
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(src)
    stem = Path(src).stem[:40]
    made = []
    for start in range(0, doc.page_count, pages_per_slice):
        end = min(start + pages_per_slice, doc.page_count) - 1
        part = pymupdf.open()
        part.insert_pdf(doc, from_page=start, to_page=end)
        path = out_dir / f"{stem}__p{start + 1:04d}-{end + 1:04d}.pdf"
        part.save(path, garbage=4, deflate=True)
        part.close()
        made.append((path, start))
    total = sum(1 for _ in range(doc.page_count))
    doc.close()
    # coverage is structural here, but assert it rather than trust the loop
    covered = set()
    for (_, start), nxt in zip(made, list(made[1:]) + [None]):
        stop = nxt[1] if nxt else total
        covered.update(range(start, stop))
    assert covered == set(range(total)), f"slice coverage gap: {total - len(covered)} pages"
    return made


def shrink_images(pdf_path, max_bytes):
    """Downscale embedded images larger than max_bytes, in place. Returns count."""
    import pymupdf

    doc = pymupdf.open(pdf_path)
    shrunk = 0
    for page in doc:
        for info in page.get_images(full=True):
            xref = info[0]
            try:
                raw = doc.extract_image(xref)["image"]
            except Exception:
                continue
            if len(raw) <= max_bytes:
                continue
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                # halve until under budget or too small to be worth it
                while len(pix.tobytes("jpeg", jpg_quality=85)) > max_bytes and min(pix.width, pix.height) > 900:
                    pix = pymupdf.Pixmap(pix, 0)  # removes alpha / normalizes
                    pix.shrink(1)
                page.replace_image(xref, stream=pix.tobytes("jpeg", jpg_quality=85))
                shrunk += 1
            except Exception as exc:  # a page we cannot shrink still parses
                print(f"    warn: could not shrink xref {xref}: {exc}")
    if shrunk:
        doc.saveIncr() if doc.can_save_incrementally() else doc.save(
            str(pdf_path) + ".tmp", garbage=4, deflate=True
        )
    doc.close()
    tmp = Path(str(pdf_path) + ".tmp")
    if tmp.exists():
        tmp.replace(pdf_path)
    return shrunk


# ---------------------------------------------------------------------------
# Parse (child process) -- isolated so CUDA memory is released on exit
# ---------------------------------------------------------------------------


def parse_one(slice_pdf_path, out_json):
    """Child mode: parse a single slice, dump its content_list, exit."""
    import asyncio

    import rag_ingest as base  # applies the required monkey-patches on import
    from raganything import RAGAnything, RAGAnythingConfig

    cfg = RAGAnythingConfig(
        working_dir=base.WORKING_DIR,
        parser="mineru",
        parse_method="auto",
        enable_image_processing=True,
        enable_table_processing=True,
        enable_equation_processing=True,
    )
    rag = RAGAnything(
        config=cfg,
        llm_model_func=base.llm_model_func,
        vision_model_func=base.vision_model_func,
        embedding_func=base.embedding_func,
    )

    async def run():
        content_list, _ = await rag.parse_document(
            file_path=str(slice_pdf_path),
            output_dir=str(BASE_DATA / "mineru_output"),
            parse_method="auto",
        )
        return content_list

    content_list = asyncio.run(run())
    Path(out_json).write_text(
        json.dumps(content_list, ensure_ascii=False), encoding="utf-8"
    )
    print(f"    parsed {len(content_list)} items -> {Path(out_json).name}")


def parse_slice_isolated(slice_path, out_json, device="cuda"):
    """Run parse_one in a fresh subprocess. Returns True on success."""
    env = dict(os.environ, MINERU_DEVICE_MODE=device, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--parse-one",
         str(slice_path), str(out_json)],
        env=env, cwd=str(HERE),
    )
    return proc.returncode == 0 and Path(out_json).exists()


# ---------------------------------------------------------------------------
# Merge + clean
# ---------------------------------------------------------------------------


def merge_content_lists(parts):
    """parts: [(content_list, page_offset)] -> one list with global page_idx.

    img_path values are already absolute (RAGAnything's parser absolutizes them
    against each slice's own output dir), so they stay resolvable after merging.
    """
    merged = []
    for content_list, offset in parts:
        for item in content_list:
            item = dict(item)
            try:
                item["page_idx"] = int(item.get("page_idx", 0)) + offset
            except (TypeError, ValueError):
                item["page_idx"] = offset
            merged.append(item)
    return merged


def clean_items(items, min_ratio=0.6):
    """Drop furniture and repair hyphenation. Returns (items, stats)."""
    stats = {"junk_items": 0, "furniture_lines": 0, "rejoined": 0, "stripped_forms": []}

    kept = [it for it in items if it.get("type") not in JUNK_TYPES]
    stats["junk_items"] = len(items) - len(kept)

    # running headers/footers: group text lines by page
    pages = {}
    for it in kept:
        if it.get("type") == "text":
            pages.setdefault(it.get("page_idx", 0), []).extend(
                it.get("text", "").splitlines()
            )
    running = find_running_lines([pages[k] for k in sorted(pages)], min_ratio)

    out = []
    for it in kept:
        if it.get("type") != "text":
            out.append(it)
            continue
        lines = [
            ln for ln in it.get("text", "").splitlines()
            if not (_is_page_number(ln) or _furniture_key(ln) in running)
        ]
        stats["furniture_lines"] += len(it.get("text", "").splitlines()) - len(lines)
        text = join_dehyphenated(lines).strip()
        if text:
            it = dict(it, text=text)
            out.append(it)

    # cross-item hyphenation: separate_content joins text items with "\n\n", so a
    # word broken at a page/slice boundary stays broken unless rejoined here
    fixed = []
    for it in out:
        if (
            fixed
            and fixed[-1].get("type") == "text"
            and it.get("type") == "text"
            and re.search(r"[A-Za-z\u00c0-\u024f]-$", fixed[-1]["text"])
        ):
            head = fixed[-1]["text"][:-1]
            fixed[-1] = dict(fixed[-1], text=head + it["text"].lstrip())
            stats["rejoined"] += 1
            continue
        fixed.append(it)

    stats["stripped_forms"] = sorted(running)
    return fixed, stats


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------


def insert_merged(content_list, source_pdf):
    import asyncio

    import rag_ingest as base
    from raganything import RAGAnything, RAGAnythingConfig

    cfg = RAGAnythingConfig(
        working_dir=base.WORKING_DIR,
        parser="mineru",
        parse_method="auto",
        enable_image_processing=True,
        enable_table_processing=True,
        enable_equation_processing=True,
    )
    rag = RAGAnything(
        config=cfg,
        llm_model_func=base.llm_model_func,
        vision_model_func=base.vision_model_func,
        embedding_func=base.embedding_func,
    )

    async def run():
        flusher = asyncio.create_task(base.periodic_cache_flush(rag))
        try:
            await rag.insert_content_list(
                content_list=content_list,
                file_path=Path(source_pdf).name,  # the SOURCE name, not a slice name
            )
        finally:
            flusher.cancel()

    asyncio.run(run())


# ---------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("source", nargs="?", help="source PDF")
    ap.add_argument("--pages", type=int, default=10, help="pages per slice")
    ap.add_argument("--max-image-mb", type=float, default=4.0)
    ap.add_argument("--parse-only", action="store_true", help="stop before insert")
    ap.add_argument("--parse-one", nargs=2, metavar=("SLICE", "OUT"))
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.parse_one:
        parse_one(*args.parse_one)
        return 0
    if not args.source:
        ap.error("source PDF required")

    src = Path(args.source).resolve()
    work = CACHE / src.stem[:40]
    slices_dir, parsed_dir = work / "slices", work / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] slicing {src.name} at {args.pages} pages/slice")
    made = slice_pdf(src, slices_dir, args.pages)
    print(f"      {len(made)} slices, coverage verified")

    print(f"[2/5] shrinking images over {args.max_image_mb}MB")
    for path, _ in made:
        n = shrink_images(path, int(args.max_image_mb * 1024 * 1024))
        if n:
            print(f"      {path.name}: shrunk {n} image(s) -> {path.stat().st_size/1048576:.1f}MB")

    print("[3/5] parsing slices (isolated subprocess each, resumable)")
    parts, failed = [], []
    for path, offset in made:
        out_json = parsed_dir / (path.stem + ".json")
        if out_json.exists():
            print(f"      cached: {path.name}")
        else:
            print(f"      parsing {path.name} (cuda)")
            if not parse_slice_isolated(path, out_json, "cuda"):
                print(f"      cuda failed, retrying {path.name} on cpu")
                if not parse_slice_isolated(path, out_json, "cpu"):
                    failed.append(path.name)
                    continue
        parts.append((json.loads(out_json.read_text(encoding="utf-8")), offset))

    if failed:
        print(f"ABORT: {len(failed)} slice(s) failed to parse: {failed}")
        print("Nothing inserted. Re-run to resume; cached slices are skipped.")
        return 2

    print("[4/5] merging + cleaning")
    merged = merge_content_lists(parts)
    cleaned, stats = clean_items(merged)
    print(f"      {len(merged)} items -> {len(cleaned)} after cleanup")
    print(f"      dropped {stats['junk_items']} junk items, "
          f"{stats['furniture_lines']} furniture lines, "
          f"rejoined {stats['rejoined']} hyphenated breaks")
    (work / "merged_content_list.json").write_text(
        json.dumps(cleaned, ensure_ascii=False), encoding="utf-8"
    )

    if args.parse_only:
        print("[5/5] --parse-only: stopping before insert")
        return 0

    print(f"[5/5] inserting as ONE document: {src.name}")
    insert_merged(cleaned, src)
    print("done")
    return 0


def self_test():
    """Runnable check of the merge + cleanup logic. No GPU, no network."""
    # page_idx offsetting
    merged = merge_content_lists([
        ([{"type": "text", "text": "a", "page_idx": 0},
          {"type": "text", "text": "b", "page_idx": 4}], 0),
        ([{"type": "text", "text": "c", "page_idx": 0}], 5),
    ])
    assert [m["page_idx"] for m in merged] == [0, 4, 5], merged

    # img_path survives the merge untouched
    m2 = merge_content_lists([([{"type": "image", "img_path": "C:/x/images/y.jpg",
                                 "page_idx": 1}], 10)])
    assert m2[0]["img_path"] == "C:/x/images/y.jpg"
    assert m2[0]["page_idx"] == 11

    # the real bug this script exists to fix: a word split across a slice boundary
    items = [
        {"type": "text", "text": "differing in the weight ratio of PCM to clinop-", "page_idx": 4},
        {"type": "text", "text": "tilolite: 80:20, 60:40.", "page_idx": 5},
    ]
    out, st = clean_items(items, min_ratio=0.99)
    assert len(out) == 1, out
    assert "clinoptilolite: 80:20" in out[0]["text"], out[0]["text"]
    assert st["rejoined"] == 1

    # a hyphen that is NOT a word break (dash at end of a non-letter) is left alone
    items = [{"type": "text", "text": "see figure 3 -", "page_idx": 0},
             {"type": "text", "text": "and table 2", "page_idx": 1}]
    out, _ = clean_items(items, min_ratio=0.99)
    assert len(out) == 2, out

    # running header on every page is stripped, real text survives
    pages = []
    for p in range(6):
        pages.append({"type": "text",
                      "text": f"Materials 2026, 19, 1888 {p + 1} of 19\nreal sentence {p}.",
                      "page_idx": p})
    out, st = clean_items(pages, min_ratio=0.6)
    assert all("Materials 2026" not in it["text"] for it in out), out
    assert any("real sentence 3" in it["text"] for it in out)
    assert st["furniture_lines"] == 6

    # digit-collapsing must NOT eat body lines that differ only by a number
    pages = []
    for p in range(6):
        pages.append({"type": "text",
                      "text": f"Materials 2026, 19, 1888 {p + 1} of 19\n"
                              f"see Figure {p + 1}\nbody text here.",
                      "page_idx": p})
    out, _ = clean_items(pages, min_ratio=0.6)
    joined = " ".join(it["text"] for it in out)
    assert "Figure 3" in joined, joined
    assert "Materials 2026" not in joined, joined

    # junk item types never reach the LLM
    out, st = clean_items([{"type": "page_number", "text": "7", "page_idx": 0},
                           {"type": "header", "text": "x", "page_idx": 0},
                           {"type": "text", "text": "kept.", "page_idx": 0}])
    assert [it["type"] for it in out] == ["text"]
    assert st["junk_items"] == 2

    # non-text modalities pass through untouched
    tbl = {"type": "table", "table_body": "<table>..</table>", "page_idx": 2}
    out, _ = clean_items([tbl])
    assert out == [tbl]

    print("self-test OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
