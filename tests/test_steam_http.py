import pytest
import requests

from collector import steam_http


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.url = "https://example.test/final"
        self.text = "<html></html>"

    def json(self):
        return {"ok": True}


@pytest.fixture
def calls(monkeypatch):
    """Replace requests.get with a queue of status codes; record how many calls were made."""
    state = {"codes": [], "n": 0, "kwargs": None}

    def fake_get(*args, **kwargs):
        state["n"] += 1
        state["kwargs"] = kwargs
        code = state["codes"].pop(0)
        if code == "timeout":
            raise requests.Timeout()
        if code == "conn":
            raise requests.ConnectionError()
        return FakeResponse(code)

    monkeypatch.setattr(steam_http.requests, "get", fake_get)
    monkeypatch.setattr(steam_http, "DELAY_SECONDS", 0)
    monkeypatch.setattr(steam_http._get.retry, "sleep", lambda s: None)
    return state


@pytest.mark.parametrize("code", [400, 404])
def test_permanent_errors_raise_immediately(calls, code):
    calls["codes"] = [code]
    with pytest.raises(steam_http.PermanentError) as e:
        steam_http.get_json("u", {})
    assert e.value.status_code == code
    assert calls["n"] == 1


@pytest.mark.parametrize("first", [429, 500, 503, "timeout", "conn"])
def test_transient_errors_are_retried(calls, first):
    calls["codes"] = [first, 200]
    assert steam_http.get_json("u", {}) == {"ok": True}
    assert calls["n"] == 2


def test_gives_up_after_5_attempts(calls):
    calls["codes"] = [503] * 5
    with pytest.raises(steam_http.TransientHTTPError):
        steam_http.get_json("u", {})
    assert calls["n"] == 5


def test_get_html_returns_text_and_final_url(calls):
    calls["codes"] = [200]
    assert steam_http.get_html("u", cookies={"a": "1"}) == ("<html></html>", "https://example.test/final")
    assert calls["kwargs"]["cookies"] == {"a": "1"}


def test_sends_user_agent_and_timeout(calls):
    calls["codes"] = [200]
    steam_http.get_json("u", {})
    assert calls["kwargs"]["headers"] == steam_http.HEADERS
    assert calls["kwargs"]["timeout"] == steam_http.TIMEOUT


def test_rate_limit_waits_only_the_remainder(monkeypatch):
    slept = []
    now = iter([100.0, 100.3, 100.3])  # first call's stamp, then check + stamp for the second call
    monkeypatch.setattr(steam_http, "_last_request_at", None)
    monkeypatch.setattr(steam_http, "DELAY_SECONDS", 1.0)
    monkeypatch.setattr(steam_http.time, "monotonic", lambda: next(now))
    monkeypatch.setattr(steam_http.time, "sleep", slept.append)
    steam_http._wait_for_rate_limit()
    steam_http._wait_for_rate_limit()
    assert slept == [pytest.approx(0.7)]
