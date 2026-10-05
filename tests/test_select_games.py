import json
from collections import Counter
from pathlib import Path

from collector.select_games import allocate, count_patches, draw, main_genre, size_group

FIXTURES = Path(__file__).parent / "fixtures"


def test_main_genre_skips_business_model_genres():
    assert main_genre(["Indie", "RPG", "Simulation"]) == "RPG"
    assert main_genre(["Free To Play", "Action"]) == "Action"


def test_main_genre_fallbacks():
    assert main_genre(["Indie"]) == "Indie"
    assert main_genre([]) == "Unknown"


def test_size_group_edges():
    assert size_group(1.0) == "1-10"
    assert size_group(10.0) == "10-100"
    assert size_group(99.9) == "10-100"
    assert size_group(100.0) == "100+"


def test_count_patches_only_counts_the_window():
    posts = json.loads((FIXTURES / "news_892970_announcements.json").read_text(encoding="utf-8"))["newsitems"]
    patch_dates = sorted(p["date"] for p in posts if "Patch" in p["title"])
    assert count_patches(posts, since=0) == 2
    assert count_patches(posts, since=patch_dates[-1]) == 1
    assert count_patches(posts, since=max(p["date"] for p in posts) + 1) == 0


def test_allocate_is_proportional_and_exact():
    quota = allocate({"a": 60, "b": 30, "c": 10}, 10)
    assert quota == {"a": 6, "b": 3, "c": 1}
    assert sum(allocate({"a": 7, "b": 7, "c": 7}, 10).values()) == 10


def test_allocate_takes_everyone_when_short():
    assert allocate({"a": 3, "b": 2}, 10) == {"a": 3, "b": 2}


def fake_rows():
    rows, appid = [], 1
    for size, n in (("1-10", 300), ("10-100", 200), ("100+", 100)):
        for i in range(n):
            rows.append({"appid": str(appid), "size": size, "main_genre": ["Action", "RPG"][i % 2],
                         "patch_active": i % 5 != 0})
            appid += 1
    return rows


def test_draw_hits_target_and_caps_big_games():
    picked = draw(fake_rows(), seed=1, target=300, cap_100_plus=40)
    assert len(picked) == 300
    assert Counter(r["size"] for r in picked)["100+"] == 40
    assert len({r["appid"] for r in picked}) == 300


def test_draw_is_reproducible_and_order_independent():
    rows = fake_rows()
    a = draw(rows, seed=7)
    b = draw(list(reversed(rows)), seed=7)
    assert [r["appid"] for r in a] == [r["appid"] for r in b]
    assert [r["appid"] for r in a] != [r["appid"] for r in draw(rows, seed=8)]


def test_cached_json_skips_calls_it_already_made(monkeypatch, tmp_path):
    from collector import select_games
    calls = []
    monkeypatch.setattr(select_games, "RAW", tmp_path)
    monkeypatch.setattr(select_games.steam_http, "get_json", lambda url, params: calls.append(url) or {"ok": 1})
    assert select_games.cached_json("x", "u", {}) == {"ok": 1}
    assert select_games.cached_json("x", "u", {}) == {"ok": 1}
    assert calls == ["u"]


def test_cached_json_caches_permanent_errors(monkeypatch, tmp_path):
    import pytest
    from collector import select_games

    def fail(url, params):
        fail.n += 1
        raise select_games.steam_http.PermanentError(404, url)
    fail.n = 0
    monkeypatch.setattr(select_games, "RAW", tmp_path)
    monkeypatch.setattr(select_games.steam_http, "get_json", fail)
    for _ in range(2):
        with pytest.raises(select_games.steam_http.PermanentError):
            select_games.cached_json("gone", "u", {})
    assert fail.n == 1


def test_step_marks_network_errors_for_retry_not_drop():
    from collector import select_games
    from collector.steam_http import PermanentError, TransientHTTPError

    def check(row):
        if row["appid"] == 1:
            raise TransientHTTPError("503 u")
        if row["appid"] == 2:
            raise PermanentError(404, "u")
        return ""
    rows = [{"appid": i, "dropped": ""} for i in (1, 2, 3)]
    assert select_games.step(rows, "check", check) == 1
    assert rows[0]["dropped"].startswith("retry:")
    assert rows[1]["error_status"] == 404 and not rows[1]["dropped"].startswith("retry:")
    assert rows[2]["dropped"] == ""

def test_draw_ignores_patch_activity():
    rows = fake_rows()
    for r in rows:
        del r["patch_active"]          # selection must work without it (D12)
    picked = draw(rows, seed=1, target=300, cap_100_plus=40)
    assert len(picked) == 300