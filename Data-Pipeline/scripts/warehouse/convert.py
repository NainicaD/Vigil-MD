"""Pure zip → NDJSON conversion for the raw load. No GCP imports.

Used by:
  scripts/warehouse/unzip_to_ndjson.py   (openFDA, and GUDID for --scope all)
  scripts/acquisition/gudid_convert.py   (GUDID, during the fetch)

Each output line is {"payload": <record>} with no other change to the record.

  openFDA  zip holds one {"meta": …, "results": [ … ]} JSON file.
           Streamed with ijson, so large MAUDE files stay out of memory.
  GUDID    zip holds N XML parts (FULLDownload_Part{n}_Of_{N}_{date}.xml).
           Streamed with iterparse; each <device> becomes one JSON record.
           Part and total counts are checked against the XML header.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile

NDJSON_PREFIX = "ndjson"
NDJSON_SUFFIX = ".ndjson.gz"   # gzip-compressed NDJSON; BigQuery load jobs read it directly
GZIP_LEVEL    = 6              # close to maximum compression, much less CPU than level 9

GUDID_NS      = "{http://www.fda.gov/cdrh/gudid}"
XSI_NS        = "{http://www.w3.org/2001/XMLSchema-instance}"
GUDID_PART_RE = re.compile(r"^FULLDownload_Part(\d+)_Of_(\d+)_.+\.xml$")


# ── Members ───────────────────────────────────────────────────────────────────

def data_members(zf: zipfile.ZipFile, source: str) -> list[str]:
    """Return the zip members that hold records, in order. Raises on anything unexpected."""
    names = [i.filename for i in zf.infolist() if not i.is_dir()]
    if source == "gudid":
        return gudid_parts(names)
    found = [n for n in names if n.endswith(".json")]
    if len(found) != 1:
        raise RuntimeError(
            f"{source}: expected exactly 1 .json member, found {len(found)}: {names}")
    return found


def gudid_parts(names: list[str]) -> list[str]:
    """Return GUDID XML parts sorted by part number. Raises unless parts 1..N are all there."""
    parts = {}
    totals = set()
    for n in names:
        m = GUDID_PART_RE.match(n.rsplit("/", 1)[-1])
        if m:
            parts[int(m.group(1))] = n
            totals.add(int(m.group(2)))
    if len(totals) != 1:
        raise RuntimeError(f"gudid: expected one 'Of_N' total, found {sorted(totals)}")
    total = totals.pop()
    missing = sorted(set(range(1, total + 1)) - set(parts))
    if missing or len(parts) != total:
        raise RuntimeError(
            f"gudid: expected parts 1..{total}, missing {missing[:10]} "
            f"(found {len(parts)})")
    return [parts[i] for i in range(1, total + 1)]


# ── GUDID XML → dict ──────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def xml_to_value(elem: ET.Element):
    """Convert one GUDID XML element to JSON-ready data.

    - xsi:nil="true" → None; leaf text is kept exactly (no trim)
    - plural wrapper whose children share one tag → {child: [ … ]}, always a list
      (identifiers/identifier, productCodes/fdaProductCode, gmdnTerms/gmdn, …)
    - any other repeated child tag → list, so no value is dropped
    - attributes (other than xsi:*) → "@name"; text next to children → "#text"
    """
    if elem.get(f"{XSI_NS}nil") == "true":
        return None
    attrs = {f"@{_local(k)}": v for k, v in elem.attrib.items()
             if not k.startswith(XSI_NS)}
    children = list(elem)
    if not children:
        if attrs:
            return {**attrs, "#text": elem.text}
        return elem.text

    tags = [_local(c.tag) for c in children]
    if _local(elem.tag).endswith("s") and len(set(tags)) == 1:
        out = {tags[0]: [xml_to_value(c) for c in children]}
    else:
        out = {}
        for child, tag in zip(children, tags):
            value = xml_to_value(child)
            if tags.count(tag) > 1:
                out.setdefault(tag, []).append(value)
            else:
                out[tag] = value
    out.update(attrs)
    if elem.text and elem.text.strip():
        out["#text"] = elem.text
    return out


def iter_gudid_part(fh, stats: dict):
    """Yield one dict per <device> in a GUDID XML part, then check the header count."""
    expected = None
    count = 0
    root = None
    for event, elem in ET.iterparse(fh, events=("start", "end")):
        if root is None:
            root = elem
            continue
        if event != "end":
            continue
        if elem.tag == f"{GUDID_NS}numberRecordXML":
            expected = int(elem.text)
        elif elem.tag == f"{GUDID_NS}numberRecordsDatabase":
            stats.setdefault("database_total", int(elem.text))
        elif elem.tag == f"{GUDID_NS}device":
            yield xml_to_value(elem)
            count += 1
            root.clear()          # keep memory flat across 25,000 devices
    if expected is None:
        raise RuntimeError("gudid: part has no <numberRecordXML> in its header")
    if count != expected:
        raise RuntimeError(f"gudid: part has {count} devices, header says {expected}")


# ── Records → NDJSON ──────────────────────────────────────────────────────────

def iter_records(fh, source: str, stats: dict):
    """Yield one dict per record from a binary zip member."""
    if source == "gudid":
        yield from iter_gudid_part(fh, stats)
    else:
        import ijson
        # use_float: without it ijson yields Decimal and json.dumps fails (G18)
        yield from ijson.items(fh, "results.item", use_float=True)


def convert_zip(zip_fileobj, out_fileobj, source: str) -> tuple[int, str]:
    """Write {"payload": record} lines to out_fileobj (text).

    Returns (row_count, content_sha256). The hash covers the canonical form
    of every record (sorted keys, no spaces), so a zip with new metadata but
    the same records gives the same hash. Raises if there are no records, or
    if GUDID counts do not match the XML header.
    """
    sha = hashlib.sha256()
    rows = 0
    stats: dict = {}
    with zipfile.ZipFile(zip_fileobj) as zf:
        for member in data_members(zf, source):
            with zf.open(member) as fh:
                for record in iter_records(fh, source, stats):
                    line = json.dumps(record, sort_keys=True, separators=(",", ":"))
                    sha.update(line.encode("utf-8"))
                    sha.update(b"\n")
                    out_fileobj.write('{"payload":' + line + "}\n")
                    rows += 1
    if rows == 0:
        raise RuntimeError(f"{source}: zip has 0 records")
    if source == "gudid" and rows != stats.get("database_total"):
        raise RuntimeError(
            f"gudid: {rows} devices, header numberRecordsDatabase says "
            f"{stats.get('database_total')}")
    return rows, sha.hexdigest()


class Utf8Writer:
    """Text adapter for a binary GCS BlobWriter, with no close() of its own.

    Use it inside `with blob.open("wb") as raw:`. On an error, BlobWriter.__exit__
    then calls terminate() and the object is never created. Do not use
    blob.open("wt"): its TextIOWrapper finalizes the upload even on an error,
    which leaves a partial NDJSON object behind.
    """
    def __init__(self, raw):
        self.raw = raw

    def write(self, text: str) -> int:
        return self.raw.write(text.encode("utf-8"))


def convert_zip_gz(zip_fileobj, raw, source: str) -> tuple[int, str]:
    """convert_zip, writing gzip-compressed NDJSON to the binary stream raw.

    The content hash is still computed by convert_zip over the uncompressed
    records, so row_count and content_sha256 match the uncompressed output.
    mtime=0 keeps the gzip bytes reproducible. GzipFile.close() writes the
    gzip trailer but does not close raw, so the caller's
    `with blob.open("wb") as raw:` still controls the upload: on an error
    BlobWriter.terminate() runs and no partial object is created.
    """
    with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=GZIP_LEVEL, mtime=0) as gz:
        return convert_zip(zip_fileobj, Utf8Writer(gz), source)


def apply_content_check(entry: dict, rows: int, content_sha: str,
                        ndjson_name: str, state_entry: dict | None) -> bool:
    """Update the pending entry. Return True if it must load."""
    entry["row_count"] = rows
    entry["content_sha256"] = content_sha
    entry["ndjson_path"] = ndjson_name
    unchanged = bool(state_entry) and state_entry.get("content_sha256") == content_sha
    entry["skip_load"] = unchanged
    return not unchanged
