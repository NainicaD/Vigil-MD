"""Build the device search index (a SQLite file) from the daily summary tables.

Reads entity_catalog (every device, product code and manufacturer with its title, company,
product codes, top brand names and report totals) and device_catalog (each device's model
numbers and UDIs), built by Data-Pipeline/scripts/warehouse/build_summaries.py. Entries use the
same entity types and ids as the query layer, so a search hit goes straight into
device_summary(entity_type, entity_id).

The file is rebuilt, not updated, and is not in git (data/search/). Search runs on it at no
cost; only this build reads BigQuery (vigil_md_prod about 0.2 GB, vigil_md_dev a few MB).

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
INDEX_VERSION = 2          # 2: built from the summary tables (entity types as in the query layer)


def load_aliases(path: Path = ALIASES_FILE) -> dict[str, list[str]]:
    return {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}


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


# ── SQLite ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE entities (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL,
  title TEXT NOT NULL, subtitle TEXT,
  title_words TEXT NOT NULL, other_words TEXT NOT NULL,
  reports INTEGER NOT NULL, deaths INTEGER NOT NULL, injuries INTEGER NOT NULL,
  product_codes TEXT NOT NULL, company_norm TEXT, data TEXT NOT NULL);
CREATE TABLE udis (udi_di TEXT PRIMARY KEY, entity_id TEXT NOT NULL);
CREATE VIRTUAL TABLE entities_fts USING fts5(id UNINDEXED, text, tokenize='trigram');
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


def write_index(path: Path, rows: list[dict], udis: list[tuple[str, str]], meta: dict) -> None:
    """Write a fresh index file (a temporary file, then renamed: never half-written)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        cols = ["id", "kind", "entity_id", "title", "subtitle", "title_words", "other_words",
                "reports", "deaths", "injuries", "product_codes", "company_norm", "data"]
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
                    help="refuse above this (vigil_md_prod about 0.2 GB)")
    a = ap.parse_args(argv)
    from google.cloud import bigquery
    client = bigquery.Client(project=PROJECT)
    sqls = {"entity_catalog": catalog_sql(a.dataset), "device_catalog": devices_sql(a.dataset)}
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
    out = a.out or index_path(a.dataset)
    kinds = {k: sum(r["kind"] == k for r in rows) for k in ("device", "manufacturer", "product_code")}
    write_index(out, rows, udis, {"dataset": a.dataset, "index_version": INDEX_VERSION,
                                  "built_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                  **kinds, "udis": len(udis)})
    print(f"=== {out}: {kinds['device']} devices, {kinds['manufacturer']} manufacturers, "
          f"{kinds['product_code']} product codes, {len(udis)} UDIs ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
