"""Check the z.ai 429 backoff wrapper in rag_ingest.py.

Run:  RAGBASE_ROOT=<any base> ZAI_API_KEY=dummy python test_zai_backoff.py

Guards the fix for the 2026-08-29 loss: a per-minute 429 (code 1302) exhausted the
openai client's sub-second retries, lightrag raised tenacity's RetryError, and
raganything silently dropped one equation - no VLM description, no entities, while
the run still reported EXITCODE=0.
"""
import asyncio, types
import rag_ingest as R
from openai import RateLimitError
from tenacity import RetryError

slept = []
async def _fake_sleep(d): slept.append(d)
R.asyncio.sleep = _fake_sleep

def _rl():
    return RateLimitError("429 code 1302",
                          response=types.SimpleNamespace(status_code=429, headers={}, request=None),
                          body=None)

class _Attempt:
    def __init__(self, e): self._e = e
    def exception(self): return self._e

def _wrapped():                       # the real-world shape lightrag surfaces
    return RetryError(_Attempt(_rl()))

def main():
    assert R._is_rate_limit(_rl())
    assert R._is_rate_limit(_wrapped()), "tenacity-wrapped 429 must be recognised"
    assert not R._is_rate_limit(ValueError("boom"))

    calls = {"n": 0}
    async def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] < 3: raise _wrapped()
        return "OK"
    R._zai_raw_complete = flaky; slept.clear()
    assert asyncio.run(R._complete_with_backoff("p")) == "OK"
    assert calls["n"] == 3 and slept == [20, 45], (calls, slept)

    async def boom(*a, **k): raise ValueError("not a 429")
    R._zai_raw_complete = boom; slept.clear()
    try:
        asyncio.run(R._complete_with_backoff("p")); assert False, "should raise"
    except ValueError: pass
    assert slept == [], "a non-429 must not sleep"

    async def always(*a, **k): raise _wrapped()
    R._zai_raw_complete = always; slept.clear()
    try:
        asyncio.run(R._complete_with_backoff("p")); assert False, "should raise"
    except RetryError: pass
    assert slept == [20, 45, 90, 150], slept

    print("ALL CHECKS PASSED")

if __name__ == "__main__":
    main()
