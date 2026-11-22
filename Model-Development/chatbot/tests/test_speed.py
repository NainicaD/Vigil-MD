"""Tests for the speed changes: BigQuery result cache, search cache, word-similarity cache and
the API's warm-up at startup (fakes only: no network, no cost)."""
import datetime as dt
import types

from fastapi.testclient import TestClient

from answer import pipeline
from api import app as api
from device_search import normalize
from query_layer.backend import Budget, BigQueryBackend
from query_layer import backend as ql_backend
from tests import test_answer
from tests.test_query_layer import FakeClient, entity_queries


class ChangingTables(FakeClient):
    """FakeClient whose tables' modified time can be moved (a daily load)."""
    modified = dt.datetime(2026, 11, 7, 6, 0)

    def get_table(self, table_id):
        return types.SimpleNamespace(modified=self.modified)


def make(client, cache_size=100, clock=None):
    now = clock or [0.0]
    return BigQueryBackend(project="p", dataset="d", client=client, cache_size=cache_size,
                           clock=lambda: now[0]), now


def test_a_repeated_query_is_served_from_the_cache_for_free():
    client = FakeClient(rows={"counts": [{"n": 1}]})
    be, _ = make(client)
    q = entity_queries()[:1]
    rows1, recs1 = be.run(q, Budget())
    budget = Budget()
    rows2, recs2 = be.run(q, budget)
    assert rows1 == rows2 and len(client.real_runs()) == 1
    assert recs2[0].billed_bytes == 0 and recs2[0].extra["cached"] is True
    assert budget.spent_gb == 0 and be.cache_hits == 1 and be.cache_misses == 1


def test_cached_rows_are_copies():
    be, _ = make(FakeClient(rows={"counts": [{"n": 1}]}))
    q = entity_queries()[:1]
    be.run(q, Budget())[0][0][0]["n"] = 999          # a caller changes its rows
    assert be.run(q, Budget())[0][0][0]["n"] == 1


def test_the_cache_expires_when_the_tables_change():
    client = ChangingTables()
    be, now = make(client)
    q = entity_queries()[:1]
    be.run(q, Budget())
    client.modified = dt.datetime(2026, 11, 8, 6, 0)  # the next daily load
    be.run(q, Budget())
    assert len(client.real_runs()) == 1               # metadata still remembered (TTL)
    now[0] += ql_backend.DATA_AS_OF_TTL_SECONDS
    be.run(q, Budget())
    assert len(client.real_runs()) == 2               # new table time: run again


def test_partial_hits_keep_the_order_and_run_only_the_rest():
    client = FakeClient(rows={"counts": [{"n": 1}], "problems": [{"p": 2}]})
    be, _ = make(client)
    qs = entity_queries()[:2]
    be.run(qs[1:], Budget())
    rows, recs = be.run(qs, Budget())
    assert [r.name for r in recs] == [q.name for q in qs]
    assert rows[1] == [{"p": 2}] and rows[0] == [{"n": 1}]
    assert [n for n, _ in client.real_runs()] == [qs[1].name, qs[0].name]
    assert not [c for n, c in client.calls if c.dry_run and n == qs[1].name][1:]   # no 2nd dry run


def test_no_cache_by_default_and_a_bounded_cache():
    client = FakeClient()
    be, _ = make(client, cache_size=0)
    q = entity_queries()[:1]
    be.run(q, Budget()); be.run(q, Budget())
    assert len(client.real_runs()) == 2
    be, _ = make(FakeClient(), cache_size=2)
    for query in entity_queries()[:4]:
        be.run([query], Budget())
    assert len(be._cache) == 2                         # oldest dropped


def test_search_cache_returns_the_same_hits_as_copies(tmp_path, monkeypatch):
    from device_search.build_index import entity_rows, write_index
    from device_search.search import DeviceIndex
    from tests import test_device_search as tds
    path = tmp_path / "t.sqlite"
    rows, udis = entity_rows(tds.CATALOG, tds.DEVICE_DETAILS, tds.ALIASES)
    write_index(path, rows, udis, {"dataset": "test"})
    idx = DeviceIndex.open(path)
    first = idx.search("dreamstation")
    assert first
    runs = []
    monkeypatch.setattr(idx, "_search", lambda *a: runs.append(a) or [])
    again = idx.search("DreamStation!")                # same words → cached, no new search
    assert again == first and not runs
    again[0].title = "changed"
    assert idx.search("dreamstation")[0].title == first[0].title   # callers get copies
    idx.search("omnipod")
    assert len(runs) == 1


def test_word_similarity_is_cached_with_the_same_values():
    normalize.word_similarity.cache_clear()
    a = normalize.word_similarity("dreamstaton", "dreamstation")
    assert normalize.word_similarity("dreamstaton", "dreamstation") == a
    assert normalize.word_similarity.cache_info().hits >= 1
    assert normalize.word_similarity("implants", "implant") == 1.0


def test_warm_up_loads_and_touches_every_part():
    calls, events = [], []
    bot = types.SimpleNamespace(
        index=types.SimpleNamespace(search=lambda q: calls.append(("search", q))),
        router=types.SimpleNamespace(classify=lambda q: calls.append(("route", q))))
    times = api.warm_up(lambda: bot, events.append)
    assert [c[0] for c in calls] == ["search", "route"]
    assert set(times) == {"open", "search", "route"}
    assert events == [{"event": "startup", "seconds": times}]


def test_the_server_loads_the_chatbot_before_the_first_request(monkeypatch):
    opened = []
    bot = test_answer.make_bot(["counts"])[0]
    bot.index.search = lambda q, limit=10: []

    def fake_open(dataset, cache_size=0):
        opened.append(cache_size)
        return bot
    monkeypatch.setattr(pipeline.Chatbot, "open", staticmethod(fake_open))
    events = []
    app = api.create_app(dataset="vigil_md_test", event_sink=events.append)
    with TestClient(app) as c:                        # runs the startup (lifespan)
        assert opened == [api.RESULT_CACHE]            # loaded before any request
        assert events[0]["event"] == "startup"
        c.get("/api/health")
    assert opened == [api.RESULT_CACHE]                # not loaded again


def test_tests_with_their_own_bot_skip_the_warm_up():
    events = []
    bot = test_answer.make_bot(["counts"])[0]
    with TestClient(api.create_app(bot=bot, dataset="t", event_sink=events.append)):
        pass
    assert events == []
