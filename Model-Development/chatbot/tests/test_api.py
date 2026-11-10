"""Tests for api/app.py: the website's HTTP API, with the chatbot's fakes (no network, no cost)."""
import pytest
from fastapi.testclient import TestClient

from answer import templates
from api import app as api
from device_search import search
from query_router import guardrails
from tests import test_answer
from tests import test_query_layer

DEV = test_answer.DEV


class FakeSearchIndex(test_answer.FakeIndex):
    """FakeIndex plus search() and meta, as DeviceIndex has."""
    meta = {"built_at": "2026-11-09T06:00:00"}

    def search(self, text, limit=10):
        del text
        return [test_answer.hit()][:limit]


def client(labels=("counts",), rows=None, fake=None, rate_limit=30, resolution=None):
    bot, fake_bq, _ = test_answer.make_bot(list(labels), resolution=resolution, rows=rows,
                                           client=fake)
    bot.index = FakeSearchIndex(
        resolution or search.Resolution("resolved", [test_answer.hit()], "dreamstation"))
    return TestClient(api.create_app(bot=bot, dataset="vigil_md_test", rate_limit=rate_limit)), fake_bq


def test_health_reports_fingerprints_and_runs_no_query():
    c, bq = client()
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["dataset"] == "vigil_md_test"
    assert body["rules_fingerprint"] == guardrails.rules_fingerprint()
    assert body["template_fingerprint"] == templates.templates_fingerprint()
    assert not bq.calls


def test_search_returns_the_contract_fields_only():
    c, bq = client()
    r = c.get("/api/search", params={"q": "dreamstation"})
    assert r.status_code == 200
    hit = r.json()["hits"][0]
    assert set(hit) == set(api.HIT_FIELDS) and hit["entity_id"] == DEV
    assert not bq.calls                       # local index, no BigQuery


@pytest.mark.parametrize("q", ["d", "x" * 201])
def test_search_rejects_too_short_or_too_long_queries(q):
    c, _ = client()
    assert c.get("/api/search", params={"q": q}).status_code == 422


def test_entity_page_has_no_sql_in_its_sources():
    c, _ = client()
    r = c.get(f"/api/entity/device/{DEV}")
    assert r.status_code == 200
    body = r.json()
    assert body["entity"]["id"] == DEV and body["counts"]["total_reports"] == 2146
    assert body["sources"] and all(set(s) == {"query", "tables"} for s in body["sources"])
    assert "SELECT" not in r.text and "@entity_id" not in r.text


def test_unknown_entity_kind_is_404_json():
    c, bq = client()
    r = c.get(f"/api/entity/gadget/{DEV}")
    assert r.status_code == 404 and r.json()["status"] == "not_found"
    assert not bq.calls


def test_entity_page_over_budget_is_a_budget_reply():
    fake = test_query_layer.FakeClient(rows=test_answer.ROWS, estimates={"counts": 0.6e9})
    c, _ = client(fake=fake)
    r = c.get(f"/api/entity/device/{DEV}")
    assert r.status_code == 413 and r.json() == {"status": "budget", "text": templates.BUDGET}


def test_answer_follows_the_chatbot_and_trims_sources():
    c, _ = client(labels=("counts",))
    r = c.post("/api/answer", json={"question": "How many deaths has the DreamStation had?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "answered" and "question" not in body
    assert [s["kind"] for s in body["sections"]] == ["headline", "counts", "footer"]
    assert all(set(s) == {"query", "tables"} for s in body["sources"])
    assert "SELECT" not in r.text


def test_answer_with_an_entity_skips_the_search():
    c, _ = client(labels=("recalls",))
    r = c.post("/api/answer", json={"question": "Was it recalled?",
                                    "entity": {"entity_type": "device", "entity_id": DEV}})
    assert r.status_code == 200 and r.json()["search"] is None


def test_advice_is_refused_first():
    c, _ = client(labels=())
    r = c.post("/api/answer", json={"question": "Should I stop using my DreamStation?"})
    assert r.json()["status"] == "refused" and r.json()["text"] == guardrails.REFUSAL


@pytest.mark.parametrize("body", [
    {"question": ""},
    {"question": "x" * 1001},
    {"question": "ok", "entity": {"entity_type": "gadget", "entity_id": DEV}},
    {},
])
def test_bad_answer_requests_are_rejected(body):
    c, bq = client()
    assert c.post("/api/answer", json=body).status_code == 422
    assert not bq.calls


def test_questions_are_rate_limited_per_client():
    c, _ = client(rate_limit=2)
    codes = [c.post("/api/answer", json={"question": "What can you do?"}).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]
    assert c.get("/api/search", params={"q": "dreamstation"}).status_code == 200   # not limited


def test_rate_limiter_window_slides():
    now = [0.0]
    lim = api.RateLimiter(2, clock=lambda: now[0])
    assert lim.allow("a") and lim.allow("a") and not lim.allow("a")
    assert lim.allow("b")                     # per client
    now[0] = 60.0
    assert lim.allow("a")


def test_public_removes_sql_everywhere():
    out = api.public({"sources": [{"query": "counts", "tables": ["t"], "sql": "SELECT 1",
                                   "params": {"x": 1}}],
                      "nested": [{"sources": [{"query": "q", "sql": "SELECT 2"}]}]})
    assert out == {"sources": [{"query": "counts", "tables": ["t"]}],
                   "nested": [{"sources": [{"query": "q", "tables": []}]}]}


@pytest.fixture
def site(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Vigil MD</title>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("not public")
    bot, _, _ = test_answer.make_bot(["counts"])
    bot.index = FakeSearchIndex(search.Resolution("resolved", [test_answer.hit()], "x"))
    return TestClient(api.create_app(bot=bot, web_dist=str(dist)))


def test_the_built_site_is_served_from_root(site):
    assert "Vigil MD" in site.get("/").text
    assert site.get("/api/health").json()["status"] == "ok"   # the API still wins under /api


def test_a_device_link_opened_or_reloaded_gets_the_page(site):
    # a single-page-app path is not a file: index.html, and the page's router shows it
    r = site.get(f"/device/device/{DEV}")
    assert r.status_code == 200 and "Vigil MD" in r.text


def test_real_files_are_served_as_files(site):
    assert site.get("/assets/app.js").text == "console.log(1)"
    assert site.get("/favicon.svg").text == "<svg/>"


def test_unknown_api_paths_stay_json_404s(site):
    r = site.get("/api/nothing-here")
    assert r.status_code == 404 and r.json()["status"] == "not_found"


def test_nothing_outside_the_build_can_be_read(site):
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        assert "not public" not in site.get(path).text, path


# ── the container (Dockerfile, ../../.dockerignore) ───────────────────────────

from pathlib import Path  # noqa: E402

CHATBOT = Path(__file__).resolve().parents[1]
REPO = CHATBOT.parents[1]


def test_the_image_copies_every_package_the_api_imports():
    dockerfile = (CHATBOT / "Dockerfile").read_text()
    for package in ("answer", "api", "device_search", "query_layer", "query_router"):
        assert f"COPY Model-Development/chatbot/{package} {package}" in dockerfile, package
    assert "COPY --from=train /build/models/baseline models/baseline" in dockerfile
    assert "COPY --from=web /web/dist web" in dockerfile    # the website, built in the image
    assert "VITE_USE_MOCKS=false" in dockerfile
    assert "--forwarded-allow-ips" in dockerfile           # per-visitor rate limit on Cloud Run
    assert "USER app" in dockerfile                       # not root


def test_the_build_context_lets_in_only_what_the_image_needs():
    lines = [l.strip() for l in (REPO / ".dockerignore").read_text().splitlines()
             if l.strip() and not l.startswith("#")]
    assert lines[0] == "*"                                # deny by default
    allowed = [l[1:] for l in lines if l.startswith("!")]
    assert not any((".env" in a and not a.endswith(".env.example")) or "credential" in a
                   or a.startswith("Data-Pipeline") for a in allowed)
    for kept_out in ("Web-App/frontend/node_modules", "Web-App/frontend/dist",
                     "Web-App/frontend/.env*"):
        assert kept_out in lines, kept_out


def test_serving_versions_match_training_versions():
    def pins(path):
        return {l.split("==")[0]: l.split("==")[1].strip() for l in path.read_text().splitlines()
                if "==" in l and not l.startswith("#")}
    train, serve = pins(CHATBOT / "requirements.txt"), pins(CHATBOT / "requirements-serve.txt")
    for lib in ("scikit-learn", "google-cloud-bigquery", "fastapi", "uvicorn"):
        assert serve[lib] == train[lib], lib
