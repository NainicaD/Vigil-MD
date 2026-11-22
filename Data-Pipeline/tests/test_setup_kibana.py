"""Tests for elk/kibana/setup_kibana.py: the Lens panels, the dashboard, the rules, and the order
of the Kibana API calls. Fake HTTP layer: no Kibana needed."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "elk" / "kibana" / "setup_kibana.py"
spec = importlib.util.spec_from_file_location("setup_kibana", SCRIPT)
K = importlib.util.module_from_spec(spec)
spec.loader.exec_module(K)


# ── Panels and dashboard ──────────────────────────────────────────────────────

@pytest.mark.parametrize("panel", K.panels(), ids=lambda p: p["id"])
def test_every_lens_panel_is_consistent(panel):
    state = panel["attributes"]["state"]
    layers = state["datasourceStates"]["formBased"]["layers"]
    for layer_id, layer in layers.items():
        assert layer["columnOrder"] == list(layer["columns"])           # every column is ordered once
        assert {"type": "index-pattern", "id": panel["references"][0]["id"],
                "name": f"indexpattern-datasource-layer-{layer_id}"} in panel["references"]
    for vis in state["visualization"]["layers"]:
        cols = layers[vis["layerId"]]["columns"]
        assert all(a in cols for a in vis["accessors"])
        assert vis.get("xAccessor", "x") in cols or vis["layerType"] == "referenceLine"
    assert panel["references"][0]["id"] in K.DATA_VIEWS


def test_panels_cover_runs_chunks_udi_check_failures_containers_and_alerts():
    queries = " ".join(p["attributes"]["state"]["query"]["query"] for p in K.panels())
    for msg in ("matching_run_metrics", "matching_chunk_scored", "matching_udi_agreement",
                "matching_task_failed", "matching_task_retry"):
        assert msg in queries
    views = {p["references"][0]["id"] for p in K.panels()}
    assert views == {K.LOGS, K.METRICS, K.ALERTS}


def test_psi_and_memory_panels_draw_the_alert_threshold():
    by_id = {p["id"]: p for p in K.panels()}
    for pid, value in (("vm-score-psi", "0.2"), ("vm-chunk-memory", "3000")):
        ref = by_id[pid]["attributes"]["state"]["datasourceStates"]["formBased"]["layers"]["ref"]
        assert ref["columns"]["r"]["params"]["value"] == value


def test_dashboard_references_every_panel_once():
    objs = K.panels()
    d = K.dashboard(objs)
    grid = json.loads(d["attributes"]["panelsJSON"])
    assert len(grid) == len(objs) == len(d["references"])
    assert [r["id"] for r in d["references"]] == [o["id"] for o in objs]
    assert {g["panelRefName"] for g in grid} == {r["name"] for r in d["references"]}


# ── Rules ─────────────────────────────────────────────────────────────────────

def test_rules_are_es_queries_that_write_to_the_alerts_index():
    for spec in K.RULES.values():
        body = K.rule_body(*spec)
        assert body["rule_type_id"] == ".es-query"
        assert body["params"]["searchConfiguration"]["index"] == K.LOGS
        (action,) = body["actions"]
        assert action["id"] == K.CONNECTOR and action["group"] == "query matched"


def test_no_run_rule_fires_when_there_is_no_record():
    body = K.rule_body(*K.RULES["vigil-md-no-matching-run"])
    p = body["params"]
    assert (p["thresholdComparator"], p["threshold"], p["timeWindowSize"], p["timeWindowUnit"]) == ("<", [1], 8, "d")


# ── main: the API calls ───────────────────────────────────────────────────────

@pytest.fixture
def api(monkeypatch):
    calls, imported = [], []

    def call(method, path, body=None, ok_missing=False, **kw):
        calls.append((method, path))
        if path == "/api/data_views":
            return {"data_view": [{"id": "random-id", "title": "vigil-md-logs-*"},
                                  {"id": "other", "title": "something-else"}]}
        return {}
    monkeypatch.setattr(K, "call", call)
    monkeypatch.setattr(K, "import_objects", lambda objs: imported.append(objs) or {"success": True})
    return calls, imported


def test_main_replaces_hand_made_views_imports_and_recreates_rules(api):
    calls, imported = api
    assert K.main() == 0
    assert ("DELETE", "/api/data_views/data_view/random-id") in calls           # same title, other id
    assert ("DELETE", "/api/data_views/data_view/other") not in calls
    assert calls.count(("POST", "/api/data_views/data_view")) == len(K.DATA_VIEWS)
    (objs,) = imported
    assert objs[-1]["type"] == "dashboard" and len(objs) == len(K.panels()) + 1
    for rule_id in K.RULES:                                                       # delete, then create
        assert calls.index(("DELETE", f"/api/alerting/rule/{rule_id}")) < calls.index(
            ("POST", f"/api/alerting/rule/{rule_id}"))
    assert calls.index(("POST", f"/api/actions/connector/{K.CONNECTOR}")) < calls.index(
        ("POST", f"/api/alerting/rule/{next(iter(K.RULES))}"))                    # connector first


def test_a_failed_import_stops_before_the_rules(api, monkeypatch):
    calls, _ = api
    monkeypatch.setattr(K, "import_objects", lambda objs: {"success": False, "errors": [{"id": "x"}]})
    with pytest.raises(SystemExit, match="dashboard import failed"):
        K.main()
    assert not any("alerting" in p for _, p in calls)
