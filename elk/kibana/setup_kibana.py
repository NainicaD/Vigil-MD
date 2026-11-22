"""Set up Kibana for the matching model: data views, the dashboard, the alert rules.

Local Kibana only (KIBANA_URL, default http://localhost:5601). Running it again gives the same
result: data views and saved objects are overwritten by fixed IDs; the alert connector and the
rules are deleted, then created again.

  data views   vigil-md-logs (vigil-md-logs-*), metricbeat (metricbeat-*), vigil-md-alerts
  dashboard    "Vigil MD matching": decision rates, score drift, throughput, chunk memory, link
               table size, UDI agreement, failed tasks and retries, container memory and CPU,
               alerts. Lens panels, built below (no hand-written ndjson file to keep in sync).
  connector    an Index connector: every alert is written to the index vigil-md-alerts
               (the basic license has no e-mail connector)
  rules        see RULES. Thresholds are starting values: change them after more runs.

Needs KIBANA_ENCRYPTION_KEY set for Kibana (docker-compose.elk.yml), or Kibana refuses the rules.

Usage (from the repo root, with ELK running):
    python3 elk/kibana/setup_kibana.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

KIBANA = os.environ.get("KIBANA_URL", "http://localhost:5601")
LOGS, METRICS, ALERTS = "vigil-md-logs", "metricbeat", "vigil-md-alerts"
DATA_VIEWS = {LOGS: "vigil-md-logs-*", METRICS: "metricbeat-*", ALERTS: "vigil-md-alerts"}
def fixed_uuid(name: str) -> str:
    """Kibana accepts a chosen ID for a connector or rule (encrypted saved objects) only if it is a
    UUID: derive a stable one from the readable name, so a rerun replaces the same object."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vigil-md/{name}"))


CONNECTOR = fixed_uuid("vigil-md-alerts-index")
DASHBOARD = "vigil-md-matching"
# The objects below are written in the 8.9+ saved-object format (Kibana 8.13 in docker-compose.elk.yml).
MIGRATION = {"coreMigrationVersion": "8.8.0", "typeMigrationVersion": "8.9.0"}

# rule key (its ID is fixed_uuid(key)) → (name, KQL on vigil-md-logs-*, time window, comparator,
# threshold, check interval)
RULES = {
    "vigil-md-no-matching-run": (
        "No matching run in 8 days", "message:matching_run_metrics", (8, "d"), "<", 1, "1d"),
    "vigil-md-score-drift": (
        "Score drift (score_psi > 0.2)", "message:matching_run_metrics and score_psi > 0.2",
        (1, "d"), ">", 0, "1h"),
    "vigil-md-task-failed": (
        "Matching task failed", "message:matching_task_failed", (15, "m"), ">", 0, "5m"),
    "vigil-md-udi-precision": (
        "UDI agreement precision below 0.95", "message:matching_udi_agreement and precision < 0.95",
        (1, "d"), ">", 0, "1d"),
    "vigil-md-memory-high": (
        "Scoring chunk above 3000 MB", "message:matching_chunk_scored and max_rss_mb > 3000",
        (15, "m"), ">", 0, "15m"),
}


# ── HTTP ──────────────────────────────────────────────────────────────────────

def call(method: str, path: str, body=None, ok_missing: bool = False, data: bytes | None = None,
         content_type: str = "application/json"):
    """One Kibana API call. ok_missing: a 404 (e.g. deleting what is not there) is not an error."""
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{KIBANA}{path}", data=data, method=method,
                                 headers={"kbn-xsrf": "true", "Content-Type": content_type})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        if ok_missing and e.code == 404:
            return None
        raise SystemExit(f"{method} {path} failed ({e.code}): {e.read().decode()[:500]}") from None


def import_objects(objects: list[dict]) -> dict:
    """Saved objects import (multipart ndjson, overwrite by id)."""
    boundary = uuid.uuid4().hex
    ndjson = "\n".join(json.dumps(o) for o in objects).encode()
    data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"vigil-md-matching.ndjson\"\r\nContent-Type: application/ndjson\r\n\r\n").encode() \
        + ndjson + f"\r\n--{boundary}--\r\n".encode()
    return call("POST", "/api/saved_objects/_import?overwrite=true", data=data,
                content_type=f"multipart/form-data; boundary={boundary}")


# ── Lens panels (pure) ────────────────────────────────────────────────────────

def col(field: str | None, op: str, label: str) -> dict:
    if op == "count":
        return {"label": label, "dataType": "number", "operationType": "count",
                "sourceField": "___records___", "isBucketed": False, "scale": "ratio"}
    return {"label": label, "dataType": "number", "operationType": op, "sourceField": field,
            "isBucketed": False, "scale": "ratio"}


def lens(obj_id: str, title: str, view: str, kql: str, metrics: dict[str, dict],
         breakdown: str | None = None, series: str = "line", ref: float | None = None) -> dict:
    """A Lens time chart: x = @timestamp, y = metrics, optional terms breakdown and a static
    reference line."""
    cols = {"x": {"label": "@timestamp", "dataType": "date", "operationType": "date_histogram",
                  "sourceField": "@timestamp", "isBucketed": True, "scale": "interval",
                  "params": {"interval": "auto"}}}
    order = ["x"]
    if breakdown:
        cols["b"] = {"label": breakdown, "dataType": "string", "operationType": "terms",
                     "sourceField": breakdown, "isBucketed": True, "scale": "ordinal",
                     "params": {"size": 10, "orderBy": {"type": "column", "columnId": next(iter(metrics))},
                                "orderDirection": "desc"}}
        order.append("b")
    cols.update(metrics)
    order += list(metrics)
    layers = {"data": {"columns": cols, "columnOrder": order, "incompleteColumns": {}}}
    vis_layers = [{"layerId": "data", "layerType": "data", "seriesType": series, "xAccessor": "x",
                   "accessors": list(metrics), **({"splitAccessor": "b"} if breakdown else {})}]
    refs = [{"type": "index-pattern", "id": view, "name": "indexpattern-datasource-layer-data"}]
    if ref is not None:
        layers["ref"] = {"columns": {"r": {"label": str(ref), "dataType": "number",
                                           "operationType": "static_value", "isStaticValue": True,
                                           "isBucketed": False, "scale": "ratio",
                                           "params": {"value": str(ref)}, "references": []}},
                         "columnOrder": ["r"], "incompleteColumns": {}}
        vis_layers.append({"layerId": "ref", "layerType": "referenceLine", "accessors": ["r"],
                           "yConfig": [{"forAccessor": "r", "axisMode": "left", "color": "#E7664C"}]})
        refs.append({"type": "index-pattern", "id": view, "name": "indexpattern-datasource-layer-ref"})
    # Without a migration version Kibana treats the object as pre-8.6 and its old Lens migration
    # fails on "formBased" ("Cannot read properties of undefined (reading 'layers')").
    return {"type": "lens", "id": obj_id, "references": refs, **MIGRATION, "attributes": {
        "title": title, "visualizationType": "lnsXY",
        "state": {"datasourceStates": {"formBased": {"layers": layers}},
                  "visualization": {"legend": {"isVisible": True, "position": "right"},
                                    "preferredSeriesType": series, "layers": vis_layers},
                  "query": {"query": kql, "language": "kuery"}, "filters": []}}}


def panels() -> list[dict]:
    run, chunk = "message:matching_run_metrics", "message:matching_chunk_scored"
    return [
        lens("vm-decision-rates", "Decision rates per run", LOGS, run,
             {"m": col("rate_match", "average", "MATCH"),
              "n": col("rate_non_match", "average", "NON_MATCH"),
              "i": col("rate_insufficient_evidence", "average", "INSUFFICIENT_EVIDENCE")}),
        lens("vm-score-psi", "Score drift (score_psi, alert above 0.2)", LOGS, run,
             {"p": col("score_psi", "max", "score_psi")}, ref=0.2),
        lens("vm-throughput", "Scoring speed (pairs per second per worker)", LOGS, run,
             {"t": col("pairs_per_s", "average", "pairs/s"), "s": col("seconds", "max", "run seconds")}),
        lens("vm-chunk-memory", "Peak memory per chunk (MB, alert above 3000)", LOGS, chunk,
             {"r": col("max_rss_mb", "max", "max_rss_mb")}, ref=3000),
        lens("vm-link-table", "Link table rows", LOGS, run,
             {"r": col("link_table_rows", "max", "rows"), "l": col("links_inserted", "max", "inserted")}),
        lens("vm-udi-agreement", "UDI agreement check (weekly)", LOGS, "message:matching_udi_agreement",
             {"p": col("precision", "average", "precision"), "r": col("recall", "average", "recall"),
              "c": col("candidate_recall", "average", "candidate recall")}),
        lens("vm-task-events", "Failed tasks and retries", LOGS,
             "message:matching_task_failed or message:matching_task_retry",
             {"c": col(None, "count", "tasks")}, breakdown="message.keyword", series="bar_stacked"),
        lens("vm-container-memory", "Container memory (bytes)", METRICS, "metricset.name:memory",
             {"m": col("docker.memory.usage.total", "max", "memory")}, breakdown="container.name"),
        lens("vm-container-cpu", "Container CPU (share of one core)", METRICS, "metricset.name:cpu",
             {"c": col("docker.cpu.total.pct", "average", "cpu")}, breakdown="container.name"),
        lens("vm-alerts", "Alerts", ALERTS, "", {"c": col(None, "count", "alerts")},
             breakdown="rule.keyword", series="bar_stacked"),
    ]


def dashboard(objs: list[dict]) -> dict:
    grid, refs = [], []
    for n, o in enumerate(objs):
        key = f"p{n}"
        grid.append({"type": "lens", "panelIndex": key, "panelRefName": f"panel_{key}",
                     "gridData": {"x": (n % 2) * 24, "y": (n // 2) * 15, "w": 24, "h": 15, "i": key},
                     "embeddableConfig": {"enhancements": {}}})
        refs.append({"type": "lens", "id": o["id"], "name": f"panel_{key}"})
    return {"type": "dashboard", "id": DASHBOARD, "references": refs, **MIGRATION, "attributes": {
        "title": "Vigil MD matching", "description": "Matching model runs, drift, resources, alerts",
        "panelsJSON": json.dumps(grid), "optionsJSON": json.dumps({"useMargins": True}),
        "timeRestore": True, "timeFrom": "now-30d", "timeTo": "now",
        "kibanaSavedObjectMeta": {"searchSourceJSON": json.dumps(
            {"query": {"query": "", "language": "kuery"}, "filter": []})}}}


# ── Rules (pure) ──────────────────────────────────────────────────────────────

def rule_body(name: str, kql: str, window: tuple[int, str], comparator: str, threshold: int,
              interval: str) -> dict:
    """An Elasticsearch query rule on vigil-md-logs-*; each alert → one document in vigil-md-alerts."""
    return {
        "name": name, "rule_type_id": ".es-query", "consumer": "stackAlerts", "tags": ["vigil-md"],
        "schedule": {"interval": interval},
        "params": {"searchType": "searchSource", "timeField": "@timestamp",
                   "timeWindowSize": window[0], "timeWindowUnit": window[1],
                   "thresholdComparator": comparator, "threshold": [threshold], "size": 10,
                   "searchConfiguration": {"query": {"query": kql, "language": "kuery"},
                                           "index": LOGS}},
        # frequency (required since 8.x): one alert document per check in which the query matches
        "actions": [{"id": CONNECTOR, "group": "query matched",
                     "frequency": {"summary": False, "notify_when": "onActiveAlert", "throttle": None},
                     "params": {"documents": [{
            "@timestamp": "{{context.date}}", "rule": name, "value": "{{context.value}}",
            "conditions": "{{context.conditions}}"}]}}],
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    existing = call("GET", "/api/data_views")["data_view"]
    for view_id, title in DATA_VIEWS.items():
        for dv in existing:            # one view per title: drop copies made by hand (other ids)
            if dv["title"] == title and dv["id"] != view_id:
                call("DELETE", f"/api/data_views/data_view/{dv['id']}", ok_missing=True)
        call("POST", "/api/data_views/data_view", {"override": True, "data_view": {
            "id": view_id, "name": view_id, "title": title, "timeFieldName": "@timestamp"}})
    print(f"data views: {', '.join(DATA_VIEWS)}")

    objs = panels()
    result = import_objects(objs + [dashboard(objs)])
    if not result.get("success"):
        raise SystemExit(f"dashboard import failed: {json.dumps(result.get('errors'))[:800]}")
    print(f"dashboard: {KIBANA}/app/dashboards#/view/{DASHBOARD} ({len(objs)} panels)")

    call("DELETE", f"/api/actions/connector/{CONNECTOR}", ok_missing=True)
    call("POST", f"/api/actions/connector/{CONNECTOR}", {
        "connector_type_id": ".index", "name": "vigil-md-alerts index",
        "config": {"index": "vigil-md-alerts", "refresh": True, "executionTimeField": "@timestamp"}})
    for name, spec in RULES.items():
        rule_id = fixed_uuid(name)
        call("DELETE", f"/api/alerting/rule/{rule_id}", ok_missing=True)
        call("POST", f"/api/alerting/rule/{rule_id}", rule_body(*spec))
    print(f"alert rules: {len(RULES)}, each alert → index vigil-md-alerts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
