"""Tests for api.deploy_check: a new revision gets traffic only if it serves what was published. No network."""
import pytest

from api import deploy_check as D

HEALTH = {"status": "ok", "dataset": "vigil_md_prod", "index_built": "2026-11-13T20:00:00+00:00",
          "model_version": "5d1584bb53b3-d232d55"}
SEARCH = {"query": "pacemaker", "hits": [{"title": "Pacemaker"}]}


def test_the_published_revision_passes():
    assert D.problems(HEALTH, SEARCH, "5d1584bb53b3-d232d55", "vigil_md_prod") == []


@pytest.mark.parametrize("health,search,problem", [
    ({**HEALTH, "model_version": "5d1584bb53b3-0000000"}, SEARCH, "router version"),
    ({**HEALTH, "model_version": None}, SEARCH, "router version"),        # trained at build time
    ({**HEALTH, "dataset": "vigil_md_dev"}, SEARCH, "dataset"),
    ({**HEALTH, "index_built": None}, SEARCH, "the search index"),
    ({**HEALTH, "status": "error"}, SEARCH, "health status"),
    (HEALTH, {"hits": []}, "search for"),
])
def test_a_wrong_revision_fails(health, search, problem):
    found = D.problems(health, search, "5d1584bb53b3-d232d55", "vigil_md_prod")
    assert len(found) == 1 and found[0].startswith(problem)


def test_main_exit_codes(monkeypatch):
    answers = {"https://x/api/health": HEALTH, "https://x/api/search": SEARCH}
    monkeypatch.setattr(D, "fetch", lambda url, token: answers[url.split("?")[0]])
    assert D.main(["--url", "https://x/", "--version", "5d1584bb53b3-d232d55"]) == D.EXIT_OK
    assert D.main(["--url", "https://x", "--version", "other"]) == D.EXIT_WRONG

    def down(url, token):
        raise ConnectionError("no answer")
    monkeypatch.setattr(D, "fetch", down)
    assert D.main(["--url", "https://x", "--version", "v"]) == D.EXIT_NO_ANSWER


def test_fetch_retries_then_gives_up(monkeypatch):
    calls = []

    def fail(req, timeout):
        calls.append(req.full_url)
        raise D.urllib.error.URLError("cold start")
    monkeypatch.setattr(D.urllib.request, "urlopen", fail)
    monkeypatch.setattr(D.time, "sleep", lambda s: None)
    with pytest.raises(ConnectionError):
        D.fetch("https://x/api/health", "t", attempts=3)
    assert len(calls) == 3
