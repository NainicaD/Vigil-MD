"""Pull 100-200 sample records from each FDA source and GUDID.

Saves raw JSON to data/raw/samples/ for use as test fixtures
during pipeline development. Does not need a GCP account.

Usage:
    python scripts/acquisition/pull_samples.py
    python scripts/acquisition/pull_samples.py --limit 100
"""
import argparse
import json
import logging
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("pull_samples")

OUT = Path("data/raw/samples")
OUT.mkdir(parents=True, exist_ok=True)

BASE = "https://api.fda.gov/device"
GUDID_BASE = "https://accessgudid.nlm.nih.gov/api/v3"

# Product code to scope MAUDE and GUDID samples.
# FRN = infusion pumps. Change this to match your chosen device category.
PRODUCT_CODE = "FRN"


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=[429, 500, 502, 503])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def pull_openfda(s, endpoint: str, limit: int, search: str = "") -> list[dict]:
    """Pull up to `limit` records from an openFDA device endpoint."""
    url = f"{BASE}/{endpoint}.json"
    params = {"limit": min(limit, 100)}   # max per request is 100
    if search:
        params["search"] = search

    records = []
    while len(records) < limit:
        params["skip"] = len(records)
        resp = s.get(url, params=params, timeout=30)

        if resp.status_code == 404:
            # No results for this search/skip combination
            break
        resp.raise_for_status()

        data = resp.json()
        batch = data.get("results", [])
        records.extend(batch)
        log.info("  %s: fetched %d so far", endpoint, len(records))

        if len(batch) < params["limit"]:
            break   # fewer results than requested → end of data
        if len(records) < limit:
            time.sleep(0.4)   # be polite between pages

    return records[:limit]


def pull_gudid(s, limit: int) -> list[dict]:
    """Search GUDID by product code and return up to `limit` device records."""
    url = f"{GUDID_BASE}/devices/search.json"
    records = []
    page = 1
    while len(records) < limit:
        params = {
            "q": PRODUCT_CODE,
            "pageNumber": page,
            "pageSize": min(100, limit - len(records)),
        }
        resp = s.get(url, params=params, timeout=30)
        if resp.status_code in (404, 400):
            break
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("deviceSearchResults", {}).get("deviceSearchResult", [])
        if not batch:
            break
        records.extend(batch)
        log.info("  gudid: fetched %d so far", len(records))
        page += 1
        time.sleep(0.4)
    return records[:limit]


def save(name: str, records: list[dict]) -> Path:
    out = OUT / f"{name}_sample.json"
    out.write_text(json.dumps({"records": records}, indent=2))
    log.info("Saved %d records → %s", len(records), out)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200,
                        help="Records to pull per source (max 200 recommended)")
    args = parser.parse_args()
    limit = min(args.limit, 200)

    s = session()

    log.info("Pulling enforcement records …")
    enforcement = pull_openfda(s, "enforcement", limit)
    save("enforcement", enforcement)

    log.info("Pulling recall records …")
    recall = pull_openfda(s, "recall", limit)
    save("recall", recall)

    log.info("Pulling MAUDE records (scoped to product code %s) …", PRODUCT_CODE)
    # Search by product code so MAUDE records are relevant to your device category
    maude = pull_openfda(
        s, "event", limit,
        search=f'device.device_report_product_code:"{PRODUCT_CODE}"'
    )
    save("maude", maude)

    log.info("Pulling GUDID records (product code %s) …", PRODUCT_CODE)
    gudid = pull_gudid(s, limit)
    save("gudid", gudid)

    # Write a summary so teammates know what's in the fixtures
    summary = {
        "product_code": PRODUCT_CODE,
        "enforcement": len(enforcement),
        "recall": len(recall),
        "maude": len(maude),
        "gudid": len(gudid),
        "note": (
            "These are development fixtures only. "
            "Not a representative sample. "
            "Replace with full scoped snapshot before modelling."
        ),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("Done. Summary: %s", summary)


if __name__ == "__main__":
    main()