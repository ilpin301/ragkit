"""Self-check for the VLM caption cache. Run: python test_caption_cache.py"""
import asyncio
import hashlib
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


def test_image_key_is_unchanged():
    h = hashlib.sha256()
    for part in (base.VISION_MODEL, "sys", "p", "IMG"):
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    assert base._caption_key("p", "sys", "IMG") == h.hexdigest()


def test_text_caption_second_call_is_a_cache_hit():
    orig_llm = base.llm_model_func
    orig_path, orig_cache = base._CAPTION_CACHE_PATH, base._caption_cache
    tmp = os.path.join(tempfile.mkdtemp(), "vlm_caption_cache.jsonl")
    try:
        calls = []

        async def fake_llm(*args, **kwargs):
            calls.append(args)
            return f"text #{len(calls)}"

        base.llm_model_func = fake_llm
        base._CAPTION_CACHE_PATH = tmp
        base._caption_cache = None

        first = asyncio.run(base.caption_llm_func("table prompt", system_prompt="S"))
        second = asyncio.run(base.caption_llm_func("table prompt", system_prompt="S"))
        assert first == second == "text #1", (first, second)
        assert len(calls) == 1, f"cached text call still hit the LLM: {len(calls)} calls"

        # A different prompt must still cost a call.
        asyncio.run(base.caption_llm_func("other prompt", system_prompt="S"))
        assert len(calls) == 2, f"distinct prompt must not hit the cache: {len(calls)}"

        # Fresh process: cache must reload from the sidecar on disk.
        base._caption_cache = None
        third = asyncio.run(base.caption_llm_func("table prompt", system_prompt="S"))
        assert third == "text #1", third
        assert len(calls) == 2, f"reloaded cache still hit the LLM: {len(calls)} calls"

        # An empty-string result must not be cached.
        empty_calls = []

        async def fake_llm_empty(*args, **kwargs):
            empty_calls.append(args)
            return ""

        base.llm_model_func = fake_llm_empty
        base._caption_cache = None
        asyncio.run(base.caption_llm_func("empty prompt", system_prompt="S"))
        asyncio.run(base.caption_llm_func("empty prompt", system_prompt="S"))
        assert len(empty_calls) == 2, f"empty result must not be cached: {len(empty_calls)}"

        # history_messages present -> must stay live, every call.
        base.llm_model_func = fake_llm
        base._caption_cache = None
        hist = [{"role": "user", "content": "x"}]
        n_before = len(calls)
        asyncio.run(base.caption_llm_func("hist prompt", system_prompt="S", history_messages=hist))
        asyncio.run(base.caption_llm_func("hist prompt", system_prompt="S", history_messages=hist))
        assert len(calls) == n_before + 2, "history_messages call must not be cached"
    finally:
        base.llm_model_func = orig_llm
        base._CAPTION_CACHE_PATH, base._caption_cache = orig_path, orig_cache


def test_text_and_image_keys_do_not_collide():
    assert base._caption_key("p", "s", None, model=base.LLM_MODEL) != base._caption_key("p", "s", None)


def test_processors_get_cached_caption_func():
    assert base._caption_func_for(base.llm_model_func) is base.caption_llm_func
    assert base._caption_func_for(base.vision_model_func) is base.vision_model_func
    assert base._mp.BaseModalProcessor.__init__ is base._bmp_init_cached


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall caption cache checks passed")
