"""Self-check for the ingest quota stop guard. Run: python test_quota_guard.py"""
import abc
import asyncio
import contextlib
import io
import os
import sys

os.environ.setdefault("RAGBASE_ROOT", os.environ.get("RAGBASE_ROOT", os.getcwd()))
os.environ.setdefault("ZAI_API_KEY", "test-key-not-used")

import rag_ingest as base


def test_reads_max_of_both_token_rows():
    payload = {"data": {"limits": [
        {"type": "TIME_LIMIT", "percentage": 99},
        {"type": "TOKENS_LIMIT", "number": 5, "percentage": 24},
        {"type": "TOKENS_LIMIT", "number": 1, "percentage": 71},
    ]}}
    rows = payload["data"]["limits"]
    pcts = [r["percentage"] for r in rows if r.get("type") == "TOKENS_LIMIT"]
    assert max(pcts) == 71, "must take the worst TOKENS_LIMIT row, ignoring TIME_LIMIT"


def test_guard_stops_at_or_above_threshold():
    stopped = []

    async def fake_stop(pct):
        stopped.append(pct)

    orig_pct, orig_stop = base._quota_pct, base._quota_stop
    try:
        base._quota_stop = fake_stop
        for pct, should_stop in ((97.9, False), (98, True), (99.5, True)):
            stopped.clear()
            base._quota_checked_at = 0.0
            base._quota_pct = lambda p=pct: p
            asyncio.run(base._quota_guard())
            assert bool(stopped) is should_stop, f"pct={pct} expected stop={should_stop}"
    finally:
        base._quota_pct, base._quota_stop = orig_pct, orig_stop


def test_guard_fails_open_when_quota_unreadable():
    stopped = []

    async def fake_stop(pct):
        stopped.append(pct)

    orig_pct, orig_stop = base._quota_pct, base._quota_stop
    try:
        base._quota_pct = lambda: None       # endpoint down / malformed
        base._quota_stop = fake_stop
        base._quota_checked_at = 0.0
        asyncio.run(base._quota_guard())
        assert not stopped, "an unreadable quota must NEVER stop a healthy ingest"
    finally:
        base._quota_pct, base._quota_stop = orig_pct, orig_stop


def test_guard_rate_limits_its_own_polling():
    calls = []

    orig_pct, orig_stop = base._quota_pct, base._quota_stop
    try:
        base._quota_pct = lambda: calls.append(1) or 0
        base._quota_stop = lambda pct: None
        base._quota_checked_at = 0.0
        asyncio.run(base._quota_guard())
        asyncio.run(base._quota_guard())      # immediately after: must be skipped
        assert len(calls) == 1, f"expected 1 poll within the window, got {len(calls)}"
    finally:
        base._quota_pct, base._quota_stop = orig_pct, orig_stop


def test_1308_is_treated_as_usage_limit():
    exc = Exception("Error code: 429 - {'error': {'code': '1308', "
                    "'message': 'Usage limit reached for 5 hour'}}")
    assert base._is_usage_limit(exc)
    assert not base._is_usage_limit(Exception("code 1302 per-minute rate limit"))


def test_flush_skips_storage_classes():
    called = []

    class LiveStore:
        async def index_done_callback(self):
            called.append("live")

    class StoreClass(abc.ABC):
        """A resolved storage CLASS, as found in vars(lr); its type is ABCMeta."""

        async def index_done_callback(self):
            called.append("class")

    class FakeLR:
        def __init__(self):
            self.live = LiveStore()
            self.resolved = StoreClass     # getattr yields the UNBOUND function
            self.label = "JsonDocStatusStorage"
            self.number = 42

    class FakeRag:
        lightrag = FakeLR()

    orig = getattr(base, "_ACTIVE_RAG", None)
    try:
        base._ACTIVE_RAG = FakeRag()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            asyncio.run(base._flush_everything())
        out = buf.getvalue()
        # Calling index_done_callback on a class raised "missing 1 required
        # positional argument: 'self'" on every quota abort before the guard.
        assert called == ["live"], f"only live instances may flush, got {called}"
        assert "flush failed" not in out, f"flush still errors: {out}"
    finally:
        base._ACTIVE_RAG = orig


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nall quota guard checks passed")
