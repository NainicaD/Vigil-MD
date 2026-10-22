"""Pull 100-200 sample records from each FDA source and GUDID.

Saves raw JSON to data/raw/samples/ for use as test fixtures
during pipeline development. Does not need a GCP account.

What changed from v1:
- GUDID: switched from product-code search (broken) to UDI-based lookup
  using UDI-DIs extracted from the MAUDE sample. This is stronger for
  the project because it gives GUDID records that are directly linked
  to real MAUDE adverse events.
- Recall: explicit product-code scope added so records are relevant.
- Enforcement: no change, was working.
- MAUDE: no change, was working.

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

OPENFDA_BASE = "https://api.fda.gov/device"
GUDID_LOOKUP  = "https://accessgudid.nlm.nih.gov/api/v3/devices/lookup.json"

# Product code to scope MAUDE and recall samples.
# FRN = infusion pumps.
# Change this to match your chosen device category:
#   https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfPCD/classification.cfm
PRODUCT_CODE = "FRN"


# ── HTTP session ──────────────────────────────────────────────────────────────

def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=2,
                  status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


# ── openFDA helpers ───────────────────────────────────────────────────────────

def pull_openfda(s: requests.Session,
                 endpoint: str,
                 limit: int,
                 search: str = "") -> list[dict]:
    """Pull up to `limit` records from an openFDA device endpoint.

    openFDA returns at most 100 records per request, so we page
    using skip until we have enough or run out of data.
    """
    url = f"{OPENFDA_BASE}/{endpoint}.json"
    records: list[dict] = []

    while len(records) < limit:
        params: dict = {
            "limit": min(100, limit - len(records)),
            "skip":  len(records),
        }
        if search:
            params["search"] = search

        try:
            resp = s.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            log.error("%s request failed: %s", endpoint, exc)
            break

        if resp.status_code == 404:
            # openFDA returns 404 when skip exceeds total results
            log.info("%s: reached end of results at %d records",
                     endpoint, len(records))
            break

        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            log.error("%s HTTP error: %s", endpoint, exc)
            break

        batch = resp.json().get("results", [])
        if not batch:
            break

        records.extend(batch)
        log.info("  %s: %d records so far", endpoint, len(records))

        if len(batch) < params["limit"]:
            # Fewer results than requested → no more pages
            break

        time.sleep(0.4)   # be polite between pages

    return records[:limit]


# ── GUDID via UDI lookup ──────────────────────────────────────────────────────

def extract_udi_dis(maude_records: list[dict]) -> list[str]:
    """Pull UDI-DI values out of the MAUDE device[] array."""
    seen: set[str] = set()
    result: list[str] = []
    for rec in maude_records:
        for dev in rec.get("device", []):
            udi = dev.get("udi_di", "").strip()
            if udi and udi not in seen:
                seen.add(udi)
                result.append(udi)
    return result


def lookup_gudid_by_udi(s: requests.Session,
                        udi_di: str):
    """Look up one DI in AccessGUDID using the correct query parameter."""
    url = "https://accessgudid.nlm.nih.gov/api/v3/devices/lookup.json"
    try:
        resp = s.get(url, params={"di": udi_di}, timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("GUDID lookup failed for %s: %s", udi_di, exc)
        return None


def pull_gudid(s: requests.Session,
               maude_records: list[dict],
               limit: int) -> list[dict]:
    """
    Look up GUDID records using UDI-DIs extracted from MAUDE.

    Why this approach:
      - The GUDID search endpoint requires specific query formats that
        vary by API version and often returns nothing for product codes.
      - UDI-based lookup is the canonical GUDID API pattern and always
        returns a result when the UDI exists.
      - Records retrieved this way are directly linked to MAUDE events,
        which is exactly the cross-source linkage the project needs.
    """
    udi_dis = extract_udi_dis(maude_records)
    log.info("Found %d unique UDI-DIs in MAUDE sample", len(udi_dis))


    if not udi_dis:
        log.warning("No UDI-DIs found in MAUDE records. "
                    "Try a different product code or expand the MAUDE limit.")
        return []

    records: list[dict] = []
    for udi in udi_dis[:limit]:
        result = lookup_gudid_by_udi(s, udi)
        if result:
            # Tag the source UDI so the schema mapper can use it
            result["_source_udi_di"] = udi
            records.append(result)
            log.info("  GUDID: %d records fetched (udi_di=%s)",
                     len(records), udi)
        time.sleep(0.3)   # NLM rate limit

    return records


# ── Persistence ───────────────────────────────────────────────────────────────

def save(name: str, records: list[dict]) -> Path:
    out = OUT / f"{name}_sample.json"
    out.write_text(json.dumps({"records": records}, indent=2))
    log.info("Saved %d records → %s", len(records), out)
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pull sample records from FDA sources and GUDID.")
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Records to pull per source (default 200, max 200 recommended)")
    args = parser.parse_args()
    limit = min(args.limit, 200)

    s = make_session()

    # ── 1. Enforcement ────────────────────────────────────────────────────────
    log.info("── Pulling enforcement records …")
    enforcement = pull_openfda(s, "enforcement", limit)
    save("enforcement", enforcement)

    # ── 2. Recall ─────────────────────────────────────────────────────────────
    # Scope by product code so records are relevant to your device category.
    # Enforcement has no product_code field so we keep all records there,
    # but recall does — filter it now.
    log.info("── Pulling recall records (product_code=%s) …", PRODUCT_CODE)
    recall = pull_openfda(
        s, "recall", limit,
        search=f'product_code:"{PRODUCT_CODE}"'
    )
    if not recall:
        # Some product codes return nothing in the recall endpoint.
        # Fall back to unfiltered so you at least have fixture data.
        log.warning("No recall records for product_code %s. "
                    "Pulling without filter as fallback.", PRODUCT_CODE)
        recall = pull_openfda(s, "recall", limit)
    save("recall", recall)

    # ── 3. MAUDE ──────────────────────────────────────────────────────────────
    log.info("── Pulling MAUDE records (product_code=%s) …", PRODUCT_CODE)
    maude = pull_openfda(
        s, "event", limit,
        search=f'device.device_report_product_code:"{PRODUCT_CODE}"'
    )
    save("maude", maude)

    # ── 4. GUDID ──────────────────────────────────────────────────────────────
    # Use UDI-DIs from MAUDE to look up GUDID records.
    # This gives us the GUDID entries that are directly linked to
    # real adverse events in our dataset.
    log.info("── Pulling GUDID records via UDI lookup …")
    gudid = pull_gudid(s, maude, limit)

    if not gudid:
        log.warning(
            "GUDID returned 0 records.\n"
            "This usually means:\n"
            "  (a) The MAUDE records for this product code lack UDI-DI values.\n"
            "  (b) The UDI-DIs present are not registered in GUDID.\n"
            "Try: run the script again with a product code that has better "
            "UDI coverage, or inspect the MAUDE sample for udi_di values."
        )
    save("gudid", gudid)

    # ── Summary ───────────────────────────────────────────────────────────────
    summary = {
        "product_code": PRODUCT_CODE,
        "enforcement":  len(enforcement),
        "recall":       len(recall),
        "maude":        len(maude),
        "gudid":        len(gudid),
        "gudid_method": "UDI-DI lookup from MAUDE records",
        "note": (
            "Development fixtures only. Not a representative sample. "
            "Replace with full scoped snapshot before modelling."
        ),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    log.info("Done. Summary: %s", summary)

    # ── Quick field audit ─────────────────────────────────────────────────────
    print("\n" + "="*60)
    print("FIELD AUDIT")
    print("="*60)
    for name, recs in [("enforcement", enforcement),
                        ("recall", recall),
                        ("maude", maude),
                        ("gudid", gudid)]:
        if recs:
            print(f"\n{name} ({len(recs)} records)")
            print("  Top-level keys:", list(recs[0].keys())[:8])
        else:
            print(f"\n{name}: EMPTY")

    # Check MAUDE UDI coverage
    if maude:
        udi_count = sum(
            1 for r in maude
            for d in r.get("device", [])
            if d.get("udi_di")
        )
        print(f"\nMAUDE UDI-DI coverage: {udi_count}/{len(maude)} records")

    # Check enforcement-recall join key
    if enforcement and recall:
        enf_nums = {r.get("recall_number") for r in enforcement
                    if r.get("recall_number")}
        rec_nums = {r.get("product_res_number") for r in recall
                    if r.get("product_res_number")}
        overlap = enf_nums & rec_nums
        print(f"\nEnforcement–Recall join key overlap "
              f"(recall_number ↔ product_res_number): {len(overlap)}")
        if overlap:
            print(f"  Sample matches: {list(overlap)[:3]}")


if __name__ == "__main__":
    main()