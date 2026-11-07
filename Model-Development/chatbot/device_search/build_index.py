"""Build the device search index (a SQLite file) from BigQuery.

One entry per:
  device        GUDID records grouped by company and brand name (a product such as
                "DreamStation" from Respironics), with all its UDIs and model numbers
  company       a manufacturer name as normalized in MAUDE
  product_code  an FDA product code, with its most reported brand names
Each entry has report counts (deaths, injuries). Device counts use exact UDI links
(MAUDE udi_di = a GUDID DI); the matching team's predicted links can be added later.

The file is rebuilt, not updated, and is not in git (data/search/). Search runs on it at no
cost; only this build reads BigQuery.

Usage (from Model-Development/chatbot/):
    python -m device_search.build_index --estimate-only                 # dry run (free)
    python -m device_search.build_index                                 # vigil_md_dev
    python -m device_search.build_index --dataset vigil_md_prod --max-gb 5   # reads 3.9 GB
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

from .normalize import norm
from .paths import index_path

PROJECT = "vigil-md-1792696816"
ALIASES_FILE = Path(__file__).with_name("aliases.json")
INDEX_VERSION = 1


def load_aliases(path: Path = ALIASES_FILE) -> dict[str, list[str]]:
    return {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}


# ── BigQuery SQL (pure) ───────────────────────────────────────────────────────

def device_sql(dataset: str, project: str = PROJECT) -> str:
    d = f"`{project}.{dataset}"
    return f"""\
WITH g AS (
  SELECT IFNULL(manufacturer_name_norm, '') AS company_norm, manufacturer_name_raw AS company,
         device_name_raw AS name,
         TRIM(REGEXP_REPLACE(LOWER(device_name_raw), r'[^a-z0-9]+', ' ')) AS brand_key,
         model_number_raw AS model, udi_di, product_codes
  FROM {d}.gudid`
  WHERE TRIM(IFNULL(device_name_raw, '')) != ''
),
devices AS (
  SELECT company_norm, brand_key,
         APPROX_TOP_COUNT(name, 1)[OFFSET(0)].value AS name,
         APPROX_TOP_COUNT(company, 1)[OFFSET(0)].value AS company,
         ARRAY_AGG(DISTINCT udi_di IGNORE NULLS) AS udi_dis,
         APPROX_TOP_COUNT(model, 20) AS top_models,
         COUNT(DISTINCT model) AS n_models,
         ARRAY_CONCAT_AGG(product_codes) AS product_codes
  FROM g
  GROUP BY company_norm, brand_key
),
linked AS (
  SELECT d.company_norm, d.brand_key,
         COUNT(DISTINCT m.source_record_id) AS reports,
         COUNT(DISTINCT IF(m.event_type = 'Death', m.source_record_id, NULL)) AS deaths,
         COUNT(DISTINCT IF(m.event_type = 'Injury', m.source_record_id, NULL)) AS injuries
  FROM devices AS d
  CROSS JOIN UNNEST(d.udi_dis) AS di
  JOIN {d}.maude` AS m ON m.udi_di = di
  GROUP BY d.company_norm, d.brand_key
)
SELECT d.company_norm, d.brand_key, d.name, d.company, d.udi_dis, d.n_models,
       ARRAY(SELECT t.value FROM UNNEST(d.top_models) AS t WHERE t.value IS NOT NULL) AS models,
       ARRAY(SELECT DISTINCT pc FROM UNNEST(d.product_codes) AS pc) AS product_codes,
       IFNULL(l.reports, 0) AS reports, IFNULL(l.deaths, 0) AS deaths,
       IFNULL(l.injuries, 0) AS injuries
FROM devices AS d
LEFT JOIN linked AS l USING (company_norm, brand_key)"""


def company_sql(dataset: str, project: str = PROJECT) -> str:
    return f"""\
SELECT manufacturer_name_norm AS company_norm,
       APPROX_TOP_COUNT(manufacturer_name_raw, 1)[OFFSET(0)].value AS company,
       COUNT(DISTINCT source_record_id) AS reports,
       COUNT(DISTINCT IF(event_type = 'Death', source_record_id, NULL)) AS deaths,
       COUNT(DISTINCT IF(event_type = 'Injury', source_record_id, NULL)) AS injuries,
       ARRAY_AGG(DISTINCT product_code IGNORE NULLS) AS product_codes
FROM `{project}.{dataset}.maude`
WHERE manufacturer_name_norm IS NOT NULL
GROUP BY manufacturer_name_norm"""


def code_sql(dataset: str, project: str = PROJECT) -> str:
    return f"""\
WITH c AS (
  SELECT product_code,
         COUNT(DISTINCT source_record_id) AS reports,
         COUNT(DISTINCT IF(event_type = 'Death', source_record_id, NULL)) AS deaths,
         COUNT(DISTINCT IF(event_type = 'Injury', source_record_id, NULL)) AS injuries,
         APPROX_TOP_COUNT(device_name_raw, 5) AS top
  FROM `{project}.{dataset}.maude`
  WHERE product_code IS NOT NULL
  GROUP BY product_code
)
SELECT product_code, reports, deaths, injuries,
       ARRAY(SELECT t.value FROM UNNEST(top) AS t WHERE t.value IS NOT NULL) AS top_names
FROM c"""


# ── SQLite (pure apart from the file) ─────────────────────────────────────────

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entities (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, subtitle TEXT,
  title_words TEXT NOT NULL, other_words TEXT NOT NULL,
  reports INTEGER NOT NULL, deaths INTEGER NOT NULL, injuries INTEGER NOT NULL,
  product_codes TEXT NOT NULL, company_norm TEXT, data TEXT NOT NULL);
CREATE TABLE udis (udi_di TEXT PRIMARY KEY, entity_id TEXT NOT NULL);
CREATE VIRTUAL TABLE entities_fts USING fts5(id UNINDEXED, text, tokenize='trigram');
"""


def device_id(company_norm: str, brand_key: str) -> str:
    return "dev:" + hashlib.sha1(f"{company_norm}|{brand_key}".encode()).hexdigest()[:12]


def alias_words(company_norm: str, aliases: dict[str, list[str]]) -> list[str]:
    """User names whose data names appear in this company name ("respironics" → philips)."""
    c = f" {norm(company_norm)} "
    return sorted({user for user, data_names in aliases.items()
                   if any(f" {norm(n)} " in c for n in data_names)})


def entity_rows(devices: list[dict], companies: list[dict], codes: list[dict],
                aliases: dict[str, list[str]]) -> tuple[list[dict], list[tuple[str, str]]]:
    """BigQuery rows → index entries and (udi_di, entity_id) pairs. Pure function.

    title_words: the words a match on counts fully (device name, model numbers, company
    name for a company, the code for a product code). other_words: words that count a
    little less (a device's company and its aliases, a code's brand names)."""
    rows, udis = [], []
    for d in devices:
        eid = device_id(d["company_norm"], d["brand_key"])
        models = [m for m in (d.get("models") or []) if m]
        codes_ = sorted(set(d.get("product_codes") or []))
        rows.append({
            "id": eid, "kind": "device", "title": d["name"].strip(), "subtitle": d.get("company") or "",
            "title_words": " ".join([norm(d["name"])] + [norm(m) for m in models]),
            "other_words": " ".join([norm(d.get("company")), norm(d["company_norm"])]
                                    + alias_words(d["company_norm"], aliases) + [c.lower() for c in codes_]),
            "reports": d["reports"], "deaths": d["deaths"], "injuries": d["injuries"],
            "product_codes": ",".join(codes_), "company_norm": d["company_norm"],
            "data": json.dumps({"models": models, "n_models": d.get("n_models", len(models)),
                                "n_udis": len(d.get("udi_dis") or [])})})
        udis += [(u, eid) for u in (d.get("udi_dis") or [])]
    for c in companies:
        codes_ = sorted(set(c.get("product_codes") or []))
        rows.append({
            "id": "co:" + c["company_norm"], "kind": "company",
            "title": c.get("company") or c["company_norm"], "subtitle": ", ".join(codes_[:5]),
            "title_words": " ".join([norm(c["company_norm"]), norm(c.get("company"))]
                                    + alias_words(c["company_norm"], aliases)),
            "other_words": "", "reports": c["reports"], "deaths": c["deaths"],
            "injuries": c["injuries"], "product_codes": ",".join(codes_),
            "company_norm": c["company_norm"], "data": "{}"})
    for p in codes:
        names = [n for n in (p.get("top_names") or []) if n]
        rows.append({
            "id": "pc:" + p["product_code"], "kind": "product_code",
            "title": f"Product code {p['product_code']}", "subtitle": "; ".join(names[:3]),
            "title_words": p["product_code"].lower(), "other_words": " ".join(norm(n) for n in names),
            "reports": p["reports"], "deaths": p["deaths"], "injuries": p["injuries"],
            "product_codes": p["product_code"], "company_norm": None,
            "data": json.dumps({"top_names": names})})
    return rows, udis


def write_index(path: Path, rows: list[dict], udis: list[tuple[str, str]], meta: dict) -> None:
    """Write a fresh index file (a temporary file, then renamed: never half-written)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        cols = ["id", "kind", "title", "subtitle", "title_words", "other_words", "reports",
                "deaths", "injuries", "product_codes", "company_norm", "data"]
        con.executemany(f"INSERT INTO entities ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                        [tuple(r[c] for c in cols) for r in rows])
        con.executemany("INSERT INTO entities_fts (id, text) VALUES (?, ?)",
                        [(r["id"], f"{r['title_words']} {r['other_words']}") for r in rows])
        con.executemany("INSERT OR IGNORE INTO udis VALUES (?, ?)", udis)
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
                    help="refuse above this (vigil_md_dev 0.07 GB; vigil_md_prod 3.9 GB)")
    a = ap.parse_args(argv)
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT)
    sqls = {"device": device_sql(a.dataset), "company": company_sql(a.dataset),
            "product_code": code_sql(a.dataset)}
    est = {k: client.query(s, job_config=bigquery.QueryJobConfig(dry_run=True)).total_bytes_processed
           for k, s in sqls.items()}
    for k, b in est.items():
        print(f"  {k:13} {b / 1e9:7.3f} GB")
    total = sum(est.values()) / 1e9
    print(f"  {'total':13} {total:7.3f} GB  (cap --max-gb {a.max_gb})")
    if a.estimate_only:
        return 0
    if total > a.max_gb:
        print("REFUSING: above --max-gb. Nothing was built.")
        return 3
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(a.max_gb * 1e9))
    data = {k: [dict(r) for r in client.query(s, job_config=cfg).result()] for k, s in sqls.items()}
    rows, udis = entity_rows(data["device"], data["company"], data["product_code"], load_aliases())
    out = a.out or index_path(a.dataset)
    write_index(out, rows, udis, {"dataset": a.dataset, "index_version": INDEX_VERSION,
                                  "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                  "devices": len(data["device"]), "companies": len(data["company"]),
                                  "product_codes": len(data["product_code"]), "udis": len(udis)})
    print(f"=== {out}: {len(data['device'])} devices, {len(data['company'])} companies, "
          f"{len(data['product_code'])} product codes, {len(udis)} UDIs ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
