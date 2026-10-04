import pytest
import requests

from collector import http


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.url = "https://example.test"


@pytest.fixture
def calls(monkeypatch):
    """Replace requests.get with a queue of status codes; record how many calls were made."""
    state = {"codes": [], "n": 0}

    def fake_get(*args, **kwargs):
        state["n"] += 1
        code = state["codes"].pop(0)
        if code == "timeout":
            raise requests.Timeout()
        return FakeResponse(code)

    monkeypatch.setattr(http.requests, "get", fake_get)
    monkeypatch.setattr(http, "DELAY_SECONDS", 0)
    monkeypatch.setattr(http.get.retry, "sleep", lambda s: None)
    return state


def test_404_is_not_retried(calls):
    calls["codes"] = [404]
    assert http.get("u").status_code == 404
    assert calls["n"] == 1


@pytest.mark.parametrize("first", [429, 500, 503, "timeout"])
def test_transient_errors_are_retried(calls, first):
    calls["codes"] = [first, 200]
    assert http.get("u").status_code == 200
    assert calls["n"] == 2


def test_gives_up_after_5_attempts(calls):
    calls["codes"] = [503] * 5
    with pytest.raises(http.TransientHTTPError):
        http.get("u")
    assert calls["n"] == 5
