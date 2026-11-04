"""Pick the newest GUDID full release from the AccessGUDID RSS feed (docs/issue.md #6, #7).

Used by check_manifest (dags/vigil_md_acquisition.py) and latest_gudid_url
(fetch_sources.py), so the change check and the fetch always agree on the snapshot.
Standard library only: the DAG can import it cheaply.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

RSS_URL = "https://accessgudid.nlm.nih.gov/download.rss?files=full"
_DATE = re.compile(r"(?<!\d)\d{8}(?!\d)")     # exactly 8 digits, e.g. 20261102


def pick_latest_gudid(rss: bytes) -> tuple[str, str]:
    """Return (url, snapshot) of the .zip link with the largest 8-digit date in its file name.

    Feed order does not matter. Raises RuntimeError if no GUDID .zip link has a date,
    instead of guessing a snapshot (a guess would re-download GUDID on every run).
    """
    found = []
    for item in ET.fromstring(rss).iter("item"):
        link = (item.findtext("link") or "").strip()
        if "gudid" in link.lower() and link.endswith(".zip"):
            m = _DATE.search(link.rsplit("/", 1)[-1])
            if m:
                found.append((m.group(0), link))
    if not found:
        raise RuntimeError("No GUDID full release with an 8-digit date in the RSS feed")
    snapshot, url = max(found)
    return url, snapshot
