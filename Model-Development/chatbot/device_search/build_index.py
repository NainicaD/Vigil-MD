"""Build the device search index (a SQLite file) from the daily summary tables.

Reads entity_catalog (every device, product code and manufacturer with its title, company,
product codes, top brand names and report totals) and device_catalog (each device's model
numbers and UDIs), built by Data-Pipeline/scripts/warehouse/build_summaries.py. Entries use the
same entity types and ids as the query layer, so a search hit goes straight into
device_summary(entity_type, entity_id).

It also stores every recall (one row per recall product, from recall joined to enforcement for
the class) in a `recalls` table, for the recall finder and its exports, with a category grouped
from the FDA root_cause by recall_categories.json. A root_cause missing from that file is stored
as "Other or unknown" and named in a warning, so a new FDA phrase never breaks a deploy.

It also stores the enforcement records (an `enforcement` table, for the "all records" file of
the search results; `scope_text` is left out: it alone would read 0.365 GB) and, for every entry,
how many recalls and enforcement records it has (from entity_recalls, so the same recalls as its
device page), so a search result can show its count from each FDA source, plus the links
themselves (`recall_links`), so a search's summary counts each recall once across its results.

The file is rebuilt, not updated, and is not in git (data/search/). Search runs on it at no
cost; only this build reads BigQuery (vigil_md_prod about 0.27 GB with the recalls, the
enforcement records and the recall links (dry run 2026-11-27), vigil_md_dev a few MB).

Usage (from Model-Development/chatbot/):
    python -m device_search.build_index --estimate-only                 # dry run (free)
    python -m device_search.build_index                                 # vigil_md_dev
    python -m device_search.build_index --dataset vigil_md_prod --max-gb 0.5
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path

from .normalize import norm
from .paths import index_path

PROJECT = "vigil-md-1792696816"
ALIASES_FILE = Path(__file__).with_name("aliases.json")
CATEGORIES_FILE = Path(__file__).with_name("recall_categories.json")
INDEX_VERSION = 4          # 2: built from the summary tables (entity types as in the query layer)
                           # 3: + recalls table (recall finder and exports)
                           # 4: + enforcement and recall_links tables, recalls/enforcement
                           #    counts per entry

# Recall categories, grouped from the FDA root_cause (recall_categories.json maps each phrase).
CATEGORIES = ("Software", "Design", "Manufacturing and process", "Component or supplier",
              "Labeling and packaging", "Other or unknown")
OTHER = "Other or unknown"
RECALL_COLUMNS = ("recall_number", "recall_class", "record_date", "recall_status", "product",
                  "firm", "reason_text", "root_cause", "action", "product_code",
                  "manufacturer_name_norm")
ENFORCEMENT_COLUMNS = ("recall_number", "event_id", "recall_class", "record_date", "status",
                       "distribution_pattern", "firm", "product", "reason_text")


def load_aliases(path: Path = ALIASES_FILE) -> dict[str, list[str]]:
    return {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}


def load_categories(path: Path = CATEGORIES_FILE) -> dict[str, str]:
    """root_cause phrase → category. Raises if a category is not one of CATEGORIES."""
    mapping = {k: v for k, v in json.loads(path.read_text(encoding="utf-8")).items()
               if not k.startswith("_")}
    bad = {v for v in mapping.values() if v not in CATEGORIES}
    if bad:
        raise ValueError(f"unknown categories in {path.name}: {sorted(bad)}")
    return mapping


# ── BigQuery SQL (pure) ───────────────────────────────────────────────────────

def catalog_sql(dataset: str, project: str = PROJECT) -> str:
    return f"""\
SELECT entity_type, entity_id, title, subtitle, company_norm, product_codes, top_names,
       reports, deaths, injuries
FROM `{project}.{dataset}.entity_catalog`"""


def devices_sql(dataset: str, project: str = PROJECT) -> str:
    return f"""\
SELECT device_id, models, n_models, udi_dis
FROM `{project}.{dataset}.device_catalog`"""


def recalls_sql(dataset: str, project: str = PROJECT) -> str:
    """Every recall product, with its class: the columns and join of entity_recalls in
    Data-Pipeline/scripts/warehouse/build_summaries.py, without the per-entity links."""
    return f"""\
SELECT r.source_record_id AS recall_number, e.recall_class, r.record_date, r.recall_status,
       r.device_name_raw AS product, r.manufacturer_name_raw AS firm, r.reason_text,
       r.root_cause, r.action, r.product_code, r.manufacturer_name_norm
FROM `{project}.{dataset}.recall` AS r
LEFT JOIN `{project}.{dataset}.enforcement` AS e ON e.source_record_id = r.source_record_id"""


def enforcement_sql(dataset: str, project: str = PROJECT) -> str:
    """The enforcement records (FDA enforcement reports), without scope_text (0.365 GB alone)."""
    return f"""\
SELECT source_record_id AS recall_number, event_id, recall_class, record_date, status,
       distribution_pattern, manufacturer_name_raw AS firm, device_name_raw AS product, reason_text
FROM `{project}.{dataset}.enforcement`"""


def recall_links_sql(dataset: str, project: str = PROJECT) -> str:
    """Which recalls belong to which entry: the links of the device pages (entity_recalls)."""
    return f"""\
SELECT DISTINCT entity_type, entity_id, recall_number
FROM `{project}.{dataset}.entity_recalls`"""


# ── SQLite ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entities (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
  title TEXT NOT NULL, subtitle TEXT,
  title_words TEXT NOT NULL, other_words TEXT NOT NULL,
  reports INTEGER NOT NULL, deaths INTEGER NOT NULL, injuries INTEGER NOT NULL,
  product_codes TEXT NOT NULL, company_norm TEXT, data TEXT NOT NULL,
  recalls INTEGER NOT NULL DEFAULT 0, enforcement INTEGER NOT NULL DEFAULT 0);
CREATE TABLE udis (udi_di TEXT PRIMARY KEY, entity_id TEXT NOT NULL);
CREATE VIRTUAL TABLE entities_fts USING fts5(id UNINDEXED, text, tokenize='trigram');
CREATE TABLE recalls (
  recall_number TEXT, recall_class TEXT, record_date TEXT, recall_status TEXT,
  product TEXT, firm TEXT, reason_text TEXT, root_cause TEXT, action TEXT,
  product_code TEXT, manufacturer_name_norm TEXT, category TEXT NOT NULL);
CREATE INDEX recalls_category ON recalls (category);
CREATE INDEX recalls_class ON recalls (recall_class);
CREATE INDEX recalls_date ON recalls (record_date);
CREATE INDEX recalls_product_code ON recalls (product_code);
CREATE INDEX recalls_manufacturer ON recalls (manufacturer_name_norm);
CREATE TABLE enforcement (
  recall_number TEXT, event_id TEXT, recall_class TEXT, record_date TEXT, status TEXT,
  distribution_pattern TEXT, firm TEXT, product TEXT, reason_text TEXT);
CREATE INDEX enforcement_recall ON enforcement (recall_number);
CREATE TABLE recall_links (entry_id TEXT NOT NULL, recall_number TEXT NOT NULL,
  PRIMARY KEY (entry_id, recall_number)) WITHOUT ROWID;
"""


def entry_id(kind: str, entity_id: str) -> str:
    """Index key: the entity type and id ("device|dev:…", "product_code|LZG")."""
    return f"{kind}|{entity_id}"


def alias_words(company_norm: str | None, aliases: dict[str, list[str]]) -> list[str]:
    """User names whose data names appear in this company name ("respironics" → philips)."""
    c = f" {norm(company_norm)} "
    return sorted({user for user, data_names in aliases.items()
                   if any(f" {norm(n)} " in c for n in data_names)})


def entity_rows(catalog: list[dict], devices: list[dict],
                aliases: dict[str, list[str]]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Catalog rows (+ device details) → index entries and (udi_di, entry id) pairs. Pure.

    title_words: words a match on counts fully (a device's name and model numbers, a
    manufacturer's names, a product code). other_words: words that count a little less (a
    device's company and its aliases, a product code's brand names)."""
    by_device = {d["device_id"]: d for d in devices}
    rows, udis = [], []
    for c in catalog:
        kind, eid = c["entity_type"], c["entity_id"]
        codes = sorted(set(c.get("product_codes") or []))
        title = (c.get("title") or eid).strip()
        subtitle = c.get("subtitle") or ""
        data: dict = {}
        if kind == "device":
            d = by_device.get(eid, {})
            models = [m for m in (d.get("models") or []) if m]
            title_words = " ".join([norm(title)] + [norm(m) for m in models])
            other_words = " ".join([norm(subtitle), norm(c.get("company_norm"))]
                                   + alias_words(c.get("company_norm"), aliases)
                                   + [x.lower() for x in codes])
            data = {"models": models, "n_models": d.get("n_models", len(models)),
                    "n_udis": len(d.get("udi_dis") or [])}
            udis += [(u, entry_id(kind, eid)) for u in (d.get("udi_dis") or [])]
        elif kind == "manufacturer":
            title_words = " ".join([norm(eid), norm(title)] + alias_words(eid, aliases))
            other_words = ""
            subtitle = ", ".join(codes[:5])
        else:                                                   # product_code
            names = [n for n in (c.get("top_names") or []) if n]
            title_words = eid.lower()
            other_words = " ".join(norm(n) for n in names)
            data = {"top_names": names}
        rows.append({"id": entry_id(kind, eid), "kind": kind, "entity_id": eid, "title": title,
                     "subtitle": subtitle, "title_words": title_words, "other_words": other_words,
                     "reports": c.get("reports") or 0, "deaths": c.get("deaths") or 0,
                     "injuries": c.get("injuries") or 0, "product_codes": ",".join(codes),
                     "company_norm": c.get("company_norm"), "data": json.dumps(data)})
    return rows, udis


def recall_rows(recalls: list[dict], categories: dict[str, str]) -> tuple[list[dict], dict[str, int]]:
    """Recall rows → index rows with a category, and the unmapped root_cause phrases with their
    row counts. A missing root_cause is "Other or unknown" without a warning. Pure."""
    rows, unmapped = [], {}
    for r in recalls:
        cause = (r.get("root_cause") or "").strip()
        category = categories.get(cause)
        if category is None:
            category = OTHER
            if cause:
                unmapped[cause] = unmapped.get(cause, 0) + 1
        date = r.get("record_date")
        rows.append({**{c: r.get(c) for c in RECALL_COLUMNS},
                     "record_date": date.isoformat() if hasattr(date, "isoformat") else date,
                     "root_cause": cause or None, "category": category})
    return rows, unmapped


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def enforcement_rows(records: list[dict]) -> list[dict]:
    """Enforcement records → index rows (dates as ISO text). Pure."""
    return [{**{c: r.get(c) for c in ENFORCEMENT_COLUMNS}, "record_date": _iso(r.get("record_date"))}
            for r in records]


def source_counts(links: list[dict], enforcement_numbers: set[str]) -> dict[str, tuple[int, int]]:
    """Index entry id → (recalls, enforcement records): distinct recalls linked to the entry,
    and how many of them have an enforcement record. Pure."""
    recalls: dict[str, set[str]] = {}
    for link in links:
        recalls.setdefault(entry_id(link["entity_type"], link["entity_id"]), set()).add(link["recall_number"])
    return {k: (len(v), len(v & enforcement_numbers)) for k, v in recalls.items()}


def with_source_counts(rows: list[dict], counts: dict[str, tuple[int, int]]) -> list[dict]:
    """Adds `recalls` and `enforcement` (0 when the entry has none) to index entries. Pure."""
    return [{**r, "recalls": counts.get(r["id"], (0, 0))[0],
             "enforcement": counts.get(r["id"], (0, 0))[1]} for r in rows]


def link_rows(links: list[dict]) -> list[tuple[str, str]]:
    """entity_recalls links → distinct (entry id, recall number) pairs. Pure."""
    return sorted({(entry_id(l["entity_type"], l["entity_id"]), l["recall_number"]) for l in links
                   if l.get("recall_number")})


def write_index(path: Path, rows: list[dict], udis: list[tuple[str, str]], meta: dict,
                recalls: list[dict] | None = None, enforcement: list[dict] | None = None,
                links: list[tuple[str, str]] | None = None) -> None:
    """Write a fresh index file (a temporary file, then renamed: never half-written).
    recalls: recall_rows() output; enforcement: enforcement_rows() output (empty when not
    given); links: link_rows() output. Entries without `recalls`/`enforcement` counts get 0."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        cols = ["id", "kind", "entity_id", "title", "subtitle", "title_words", "other_words",
                "reports", "deaths", "injuries", "product_codes", "company_norm", "data",
                "recalls", "enforcement"]
        con.executemany(f"INSERT INTO entities ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [tuple(r.get(c, 0) if c in ("recalls", "enforcement") else r[c] for c in cols)
                         for r in rows])
        con.executemany("INSERT INTO entities_fts (id, text) VALUES (?, ?)",
                        [(r["id"], f"{r['title_words']} {r['other_words']}") for r in rows])
        con.executemany("INSERT OR IGNORE INTO udis VALUES (?, ?)", udis)
        rcols = [*RECALL_COLUMNS, "category"]
        con.executemany(f"INSERT INTO recalls ({','.join(rcols)}) VALUES ({','.join('?' * len(rcols))})",
                        [tuple(r[c] for c in rcols) for r in recalls or []])
        ecols = list(ENFORCEMENT_COLUMNS)
        con.executemany(f"INSERT INTO enforcement ({','.join(ecols)}) VALUES ({','.join('?' * len(ecols))})",
                        [tuple(r[c] for c in ecols) for r in enforcement or []])
        con.executemany("INSERT INTO recall_links VALUES (?, ?)", links or [])
        con.executemany("INSERT INTO meta VALUES (?, ?)", [(k, str(v)) for k, v in meta.items()])
        con.commit()
    finally:
        con.close()
    tmp.replace(path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", default="vigil_md_dev")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--estimate-only", action="store_true")
    ap.add_argument("--max-gb", type=float, default=0.5,
                    help="refuse above this (vigil_md_prod about 0.2 GB)")
    a = ap.parse_args(argv)
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT)
    sqls = {"entity_catalog": catalog_sql(a.dataset), "device_catalog": devices_sql(a.dataset),
            "recall": recalls_sql(a.dataset), "enforcement": enforcement_sql(a.dataset),
            "entity_recalls": recall_links_sql(a.dataset)}
    est = {k: client.query(s, job_config=bigquery.QueryJobConfig(dry_run=True)).total_bytes_processed
           for k, s in sqls.items()}
    for k, b in est.items():
        print(f"  {k:15} {b / 1e9:7.3f} GB")
    total = sum(est.values()) / 1e9
    print(f"  {'total':15} {total:7.3f} GB  (cap --max-gb {a.max_gb})")
    if a.estimate_only:
        return 0
    if total > a.max_gb:
        print("REFUSING: above --max-gb. Nothing was built.")
        return 3
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(a.max_gb * 1e9))
    data = {k: [dict(r) for r in client.query(s, job_config=cfg).result()] for k, s in sqls.items()}
    rows, udis = entity_rows(data["entity_catalog"], data["device_catalog"], load_aliases())
    enforcement = enforcement_rows(data["enforcement"])
    rows = with_source_counts(rows, source_counts(data["entity_recalls"],
                                                  {e["recall_number"] for e in enforcement}))
    recalls, unmapped = recall_rows(data["recall"], load_categories())
    for cause, n in sorted(unmapped.items(), key=lambda kv: -kv[1]):
        print(f"WARNING: root_cause {cause!r} ({n} recalls) is not in {CATEGORIES_FILE.name}; "
              f"stored as {OTHER!r}. Add it to the file.")
    out = a.out or index_path(a.dataset)
    kinds = {k: sum(r["kind"] == k for r in rows) for k in ("device", "manufacturer", "product_code")}
    write_index(out, rows, udis, {"dataset": a.dataset, "index_version": INDEX_VERSION,
                                  "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                  **kinds, "udis": len(udis), "recalls": len(recalls),
                                  "enforcement": len(enforcement)},
                recalls, enforcement, link_rows(data["entity_recalls"]))
    print(f"=== {out}: {kinds['device']} devices, {kinds['manufacturer']} manufacturers, "
          f"{kinds['product_code']} product codes, {len(udis)} UDIs, {len(recalls)} recalls, "
          f"{len(enforcement)} enforcement records "
          f"({len(unmapped)} unmapped root_cause phrases) ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
