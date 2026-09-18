"""Self-check for the VLM caption cache. Run: python test_caption_cache.py"""
import asyncio
import os
import sys
import tempfile

os.environ.setdefault("RAGBASE_ROOT", os.environ.get("RAGBASE_ROOT", os.getcwd()))
os.environ.setdefault("ZAI_API_KEY", "test-key-not-used")

import rag_ingest as base


def _with_stub(calls):
    async def fake_complete(*args, **kwargs):
        calls.append(kwargs.get("messages"))
        return f"caption #{len(calls)}"
    return fake_complete


def test_second_call_is_a_cache_hit():
    calls = []
    orig = base.openai_complete_if_cache
    orig_path, orig_cache = base._CAPTION_CACHE_PATH, base._caption_cache
    tmp = os.path.join(tempfile.mkdtemp(), "vlm_caption_cache.jsonl")
    try:
        base.openai_complete_if_cache = _with_stub(calls)
        base._CAPTION_CACHE_PATH = tmp
        base._caption_cache = None

        first = asyncio.run(base.vision_model_func("describe", image_data="AAAA"))
        second = asyncio.run(base.vision_model_func("describe", image_data="AAAA"))
        assert first == second == "caption #1", (first, second)
        assert len(calls) == 1, f"cached call still hit the VLM: {len(calls)} calls"

        # A different image must still cost a call.
        asyncio.run(base.vision_model_func("describe", image_data="BBBB"))
        assert len(calls) == 2, f"distinct image must not hit the cache: {len(calls)}"

        # Fresh process: cache must reload from the sidecar on disk.
        base._caption_cache = None
        third = asyncio.run(base.vision_model_func("describe", image_data="AAAA"))
        assert third == "caption #1", third
        assert len(calls) == 2, f"reloaded cache still hit the VLM: {len(calls)} calls"
    finally:
        base.openai_complete_if_cache = orig
        base._CAPTION_CACHE_PATH, base._caption_cache = orig_path, orig_cache


def test_query_path_is_never_cached():
    calls = []
    orig = base.openai_complete_if_cache
    orig_path, orig_cache = base._CAPTION_CACHE_PATH, base._caption_cache
    tmp = os.path.join(tempfile.mkdtemp(), "vlm_caption_cache.jsonl")
    try:
        base.openai_complete_if_cache = _with_stub(calls)
        base._CAPTION_CACHE_PATH = tmp
        base._caption_cache = None
        msgs = [{"role": "user", "content": "hi"}]
        asyncio.run(base.vision_model_func("", messages=msgs))
        asyncio.run(base.vision_model_func("", messages=msgs))
        assert len(calls) == 2, "query-time multimodal must stay live, not cached"
        assert not os.path.exists(tmp), "query path must not write the caption sidecar"
    finally:
        base.openai_complete_if_cache = orig
        base._CAPTION_CACHE_PATH, base._caption_cache = orig_path, orig_cache


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall caption cache checks passed")
