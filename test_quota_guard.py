"""Self-check for the ingest quota stop guard. Run: python test_quota_guard.py"""
import abc
import asyncio
import contextlib
import io
import json
import os
import sys
import time

os.environ.setdefault("RAGBASE_ROOT", os.environ.get("RAGBASE_ROOT", os.getcwd()))
os.environ.setdefault("ZAI_API_KEY", "test-key-not-used")

from tenacity import RetryError

import rag_ingest as base


FUTURE = (time.time() + 3600) * 1000
PAST = (time.time() - 3600) * 1000


def payload(*pcts, reset=None, msg="ok"):
    rows = [{"type": "TIME_LIMIT", "percentage": 99}]
    rows += [{"type": "TOKENS_LIMIT", "percentage": p, "nextResetTime": reset or FUTURE}
             for p in pcts]
    return json.dumps({"code": 200, "msg": msg, "data": {"limits": rows}})


@contextlib.contextmanager
def patched(**kw):
    """Swap attributes on rag_ingest, restore on exit. Never touches the network."""
    orig = {k: getattr(base, k) for k in kw}
    try:
        for k, v in kw.items():
            setattr(base, k, v)
        yield
    finally:
        for k, v in orig.items():
            setattr(base, k, v)


def fake_get(script):
    """_quota_http_get stand-in: pops one (status, body) or exception per call."""
    urls = []

    def get(url):
        urls.append(url)
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    get.urls = urls
    return get


def run_quiet(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        out = fn(*a)
    return out, buf.getvalue()


def fake_pct(*readings):
    """_quota_pct stand-in returning readings in order; records the host tuples asked for."""
    calls = []

    def pct(hosts=base._QUOTA_HOSTS):
        calls.append(hosts)
        return readings[len(calls) - 1]
    pct.calls = calls
    return pct


class Stopped(Exception):
    pass


def raising_stop(stopped):
    async def stop(pct):
        stopped.append(pct)
        raise Stopped
    return stop


def test_valid_payload_reads_max_of_both_token_rows():
    get = fake_get([(200, payload(24, 71))])
    with patched(_quota_http_get=get):
        pct, out = run_quiet(base._quota_pct)
    assert pct == 71, "must take the worst TOKENS_LIMIT row, ignoring TIME_LIMIT"
    assert "QUOTA_CHECK_FAILED" not in out
    assert get.urls == ["https://api.z.ai/api/monitor/usage/quota/limit"]


def test_empty_body_is_logged_and_falls_through():
    get = fake_get([(200, ""), (200, "<html>gateway</html>"), (200, payload(33))])
    with patched(_quota_http_get=get):
        pct, out = run_quiet(base._quota_pct)
    assert pct == 33
    assert [u.split("/")[2] for u in get.urls] == ["api.z.ai", "api.z.ai", "open.bigmodel.cn"]
    lines = [l for l in out.splitlines() if l.startswith("QUOTA_CHECK_FAILED:")]
    assert len(lines) == 2, out
    assert "host=api.z.ai" in lines[0] and "HTTP 200" in lines[0] and "elapsed=" in lines[0]
    assert "gateway" in lines[1], "the body prefix must be in the line"


def test_timeout_twice_on_zai_uses_bigmodel():
    get = fake_get([TimeoutError("The read operation timed out"),
                    TimeoutError("The read operation timed out"),
                    (200, payload(48))])
    with patched(_quota_http_get=get):
        pct, out = run_quiet(base._quota_pct)
    assert pct == 48
    assert out.count("QUOTA_CHECK_FAILED:") == 2 and "TimeoutError" in out


def test_all_hosts_fail_returns_none_and_never_stops():
    get = fake_get([(500, "x"), TimeoutError("t"), (200, "")])
    stopped = []
    with patched(_quota_http_get=get, _quota_stop=raising_stop(stopped)):
        pct, out = run_quiet(base._quota_pct)
        assert pct is None
        assert "quota check failed on all hosts; continuing" in out
        base._quota_checked_at = 0.0
        get2 = fake_get([(500, "x")] * 3)
        base._quota_http_get = get2
        run_quiet(lambda: asyncio.run(base._quota_guard()))
    assert not stopped, "unreadable monitor must fail open"


def test_non_ascii_body_does_not_crash_logging():
    chinese = json.dumps({"code": 500, "msg": "系统错误"}, ensure_ascii=False)
    get = fake_get([(200, chinese), (200, chinese), (200, chinese)])
    cp1251 = io.TextIOWrapper(io.BytesIO(), encoding="cp1251", errors="strict")
    with patched(_quota_http_get=get), contextlib.redirect_stdout(cp1251):
        assert base._quota_pct() is None      # strict cp1251 would raise on a raw print
        cp1251.flush()


def test_invalid_readings_are_rejected():
    bad = [
        (200, payload(150)),                       # out of range
        (200, payload(-1)),
        (200, payload(40, reset=PAST)),            # stale reset time
        (200, json.dumps({"data": {"limits": [{"type": "TIME_LIMIT", "percentage": 5}]}})),
        (200, json.dumps({"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": "40",
                                               "nextResetTime": FUTURE}]}})),
        (503, payload(40)),                        # valid body, wrong status
    ]
    for status, body in bad:
        try:
            base._quota_parse(status, body)
        except Exception:
            continue
        raise AssertionError(f"accepted invalid reading: {status} {body[:80]}")
    assert base._quota_parse(200, payload(0, 100)) == 100


def test_guard_stops_only_when_confirmed():
    cases = [                       # (first, confirm, stops)
        (99, 50, False),
        (99, 99, True),
        (98, 98.5, True),
        (99, None, False),
    ]
    for first, again, should_stop in cases:
        stopped = []
        pct = fake_pct(first, again)
        with patched(_quota_pct=pct, _quota_stop=raising_stop(stopped)):
            base._quota_checked_at = 0.0
            try:
                run_quiet(lambda: asyncio.run(base._quota_guard()))
            except Stopped:
                pass
            assert bool(stopped) is should_stop, (first, again)
            assert pct.calls[1] == base._QUOTA_CONFIRM_HOSTS
            assert base._QUOTA_CONFIRM_HOSTS[0] == "open.bigmodel.cn", "confirm prefers the other host"
            if not should_stop:
                assert base._quota_checked_at == 0.0, "unconfirmed reading must reset the poll timer"


def test_guard_below_threshold_takes_no_confirm_read():
    stopped = []
    pct = fake_pct(97.9)
    with patched(_quota_pct=pct, _quota_stop=raising_stop(stopped)):
        base._quota_checked_at = 0.0
        asyncio.run(base._quota_guard())
    assert not stopped and len(pct.calls) == 1


def test_guard_fails_open_when_quota_unreadable():
    stopped = []
    with patched(_quota_pct=fake_pct(None), _quota_stop=raising_stop(stopped)):
        base._quota_checked_at = 0.0
        asyncio.run(base._quota_guard())
    assert not stopped, "an unreadable quota must NEVER stop a healthy ingest"


def test_guard_rate_limits_its_own_polling():
    pct = fake_pct(0, 0)
    with patched(_quota_pct=pct, _quota_stop=raising_stop([])):
        base._quota_checked_at = 0.0
        asyncio.run(base._quota_guard())
        asyncio.run(base._quota_guard())      # immediately after: must be skipped
    assert len(pct.calls) == 1, f"expected 1 poll within the window, got {len(pct.calls)}"


def test_preflight_confirm_logic():
    for readings, want, n_reads in (((99, 99), 99, 2), ((99, 50), None, 2),
                                    ((99, None), None, 2), ((97.9,), None, 1),
                                    ((None,), None, 1)):
        pct = fake_pct(*readings)
        with patched(_quota_pct=pct):
            got, _ = run_quiet(base._quota_confirmed_pct)
        assert got == want and len(pct.calls) == n_reads, (readings, got)


def test_1308_is_treated_as_usage_limit():
    exc = Exception("Error code: 429 - {'error': {'code': '1308', "
                    "'message': 'Usage limit reached for 5 hour'}}")
    assert base._is_usage_limit(exc)
    assert not base._is_usage_limit(Exception("code 1302 per-minute rate limit"))


def _api_error(code, message="x", nested=True):
    e = Exception(f"Error code: 429 - {message}")
    e.body = {"error": {"code": code, "message": message}} if nested else {"code": code}
    return e


def test_1308_code_parsing_is_precise():
    assert base._is_usage_limit(_api_error("1308"))
    assert base._is_usage_limit(_api_error(1308, nested=False))
    assert not base._is_usage_limit(_api_error("1302"))
    # request id containing 1308 in the message, real code 1302: no match
    msg = "Rate limit, request id 202610161308026eb3fa55740a4634"
    assert not base._is_usage_limit(_api_error("1302", msg))
    assert not base._is_usage_limit(Exception(f"Error code: 429 - {msg}"))
    assert not base._is_usage_limit(Exception("Error code: 429 - {'error': {'code': '1302', "
                                              f"'message': '{msg}'}}}}"))

    class Attempt:
        def __init__(self, e): self._e = e
        def exception(self): return self._e
    assert base._is_usage_limit(RetryError(Attempt(_api_error("1308"))))
    assert not base._is_usage_limit(RetryError(Attempt(_api_error("1302"))))


def test_every_zai_stop_code_is_detected():
    for code in sorted(base._ZAI_STOP_CODES):
        assert base._is_usage_limit(_api_error(code)), code
        assert base._is_usage_limit(_api_error(int(code), nested=False)), code
        assert base._is_usage_limit(Exception(f"Error code: 429 - {{'error': {{'code': '{code}', "
                                              "'message': 'limit'}}}}")), code
    assert base._ZAI_STOP_CODES == {"1113", "1308", "1309", "1310", "1314", "1316", "1317",
                                    "1318", "1319", "1320", "1321"}
    for code in ("1302", "1305", "1311", "1313"):
        assert not base._is_usage_limit(_api_error(code)), code
        assert not base._is_usage_limit(Exception(f"Error code: 429 - {{'error': {{'code': "
                                                  f"'{code}', 'message': 'x'}}}}")), code
    # request id containing 1309, real code 1305: no match
    msg = "Overloaded, request id 202610161309026eb3fa55740a4634"
    assert not base._is_usage_limit(_api_error("1305", msg))


def _backoff_run(errors, monitor):
    """Run _complete_with_backoff against `errors`; returns (result, slept, stopped, pct reads)."""
    slept, stopped, queue = [], [], list(errors)

    async def raw(*a, **k):
        if queue:
            raise queue.pop(0)
        return "OK"

    async def fake_sleep(d):
        slept.append(d)

    pct = fake_pct(*monitor)
    real_sleep = base.asyncio.sleep
    base.asyncio.sleep = fake_sleep
    try:
        with patched(_zai_raw_complete=raw, _quota_pct=pct, _quota_stop=raising_stop(stopped)):
            base._quota_checked_at = time.monotonic()      # skip the guard's own read
            try:
                res, _ = run_quiet(lambda: asyncio.run(base._complete_with_backoff("p")))
            except Stopped:
                res = "STOPPED"
    finally:
        base.asyncio.sleep = real_sleep
    return res, slept, stopped, pct.calls


def test_backoff_1308_with_healthy_monitor_retries_once():
    res, slept, stopped, reads = _backoff_run([_api_error("1308")], [40])
    assert res == "OK" and slept == [20] and not stopped and len(reads) == 1
    # a SECOND 1308 in the same call stops unconditionally, without another read
    res, slept, stopped, reads = _backoff_run([_api_error("1308"), _api_error("1308")], [40, 40])
    assert res == "STOPPED" and stopped == [100] and slept == [20] and len(reads) == 1


def test_backoff_1308_with_unreadable_monitor_stops():
    res, slept, stopped, _ = _backoff_run([_api_error("1308")], [None])
    assert res == "STOPPED" and stopped == [100] and slept == []
    res, slept, stopped, _ = _backoff_run([_api_error("1308")], [99])
    assert res == "STOPPED" and stopped == [100]


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
