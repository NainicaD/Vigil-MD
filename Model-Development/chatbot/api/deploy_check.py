"""Check a new Cloud Run revision before it gets traffic (CI deploy job).

The deploy job starts the new revision with no traffic under its own tag URL, then runs this. It
passes only if /api/health answers with the router version that was just published, the expected
dataset and a built search index, and /api/search finds something. On a failure the job stops
before moving traffic, so the revision that was serving keeps serving.

Standard library only (no install needed). The private service needs an identity token, read from
the environment variable ID_TOKEN.

Usage (from Model-Development/chatbot/):
    ID_TOKEN=... python -m api.deploy_check --url https://<tag>---vigil-md-....run.app \
        --version 5d1584bb53b3-d232d55 --dataset vigil_md_prod

Exit codes: 0 the revision is good; 1 it answered, but wrongly; 3 it did not answer.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SEARCH_QUERY = "pacemaker"         # a common device: any real index has hits for it
EXIT_OK, EXIT_WRONG, EXIT_NO_ANSWER = 0, 1, 3


def problems(health: dict, search: dict, version: str, dataset: str) -> list[str]:
    """Why the revision is not the one we meant to deploy; empty if it is. Pure."""
    out = []
    if health.get("status") != "ok":
        out.append(f"health status {health.get('status')!r}, expected 'ok'")
    if health.get("model_version") != version:
        out.append(f"router version {health.get('model_version')!r}, expected {version!r}")
    if health.get("dataset") != dataset:
        out.append(f"dataset {health.get('dataset')!r}, expected {dataset!r}")
    if not health.get("index_built"):
        out.append("the search index reports no build time")
    if not search.get("hits"):
        out.append(f"search for {SEARCH_QUERY!r} found nothing")
    return out


def fetch(url: str, token: str, attempts: int = 4, wait: float = 15.0, timeout: float = 90.0) -> dict:
    """GET url as JSON. Retries, because the first request starts a cold instance (25-40 s)."""
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    last = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            last = e
            if i + 1 < attempts:
                time.sleep(wait)
    raise ConnectionError(f"{url}: no answer after {attempts} attempts ({last})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="the new revision's tag URL")
    ap.add_argument("--version", required=True, help="the router version that should be served")
    ap.add_argument("--dataset", default="vigil_md_prod")
    a = ap.parse_args(argv)
    token = os.environ.get("ID_TOKEN", "")
    base = a.url.rstrip("/")
    try:
        health = fetch(f"{base}/api/health", token)
        search = fetch(f"{base}/api/search?" + urllib.parse.urlencode({"q": SEARCH_QUERY, "limit": 3}), token)
    except ConnectionError as e:
        print(f"FAIL {e}", file=sys.stderr)
        return EXIT_NO_ANSWER
    print("health:", json.dumps(health))
    print(f"search {SEARCH_QUERY!r}: {len(search.get('hits', []))} hits")
    bad = problems(health, search, a.version, a.dataset)
    for p in bad:
        print(f"FAIL {p}", file=sys.stderr)
    if bad:
        return EXIT_WRONG
    print("PASS the new revision serves the published router")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
