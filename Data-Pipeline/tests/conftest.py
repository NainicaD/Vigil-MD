import io
import json
import sys
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def no_logstash(monkeypatch):
    """Keep test logs out of Logstash (scripts/logger.py sends to localhost:5044).

    Covers code that runs in the test process. Scripts started with subprocess
    are not covered, so subprocess tests use --print-only (which does not log).
    """
    from scripts import logger
    monkeypatch.setattr(logger.LogstashHandler, "emit", lambda self, record: None)


@pytest.fixture
def fake_gcp(monkeypatch):
    """Replace google.cloud.storage / bigquery so no test touches real GCP."""
    storage = MagicMock(name="storage")
    bigquery = MagicMock(name="bigquery")
    bigquery.SourceFormat.PARQUET = "PARQUET"
    bigquery.WriteDisposition.WRITE_TRUNCATE = "WRITE_TRUNCATE"

    cloud = types.ModuleType("google.cloud")
    cloud.storage = storage
    cloud.bigquery = bigquery
    google = types.ModuleType("google")
    google.cloud = cloud
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.storage", storage)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", bigquery)

    # BigQuery tables report 10 rows / 3 columns unless a test overrides it.
    table = bigquery.Client.return_value.get_table.return_value
    table.num_rows = 10
    table.schema = ["a", "b", "c"]
    return types.SimpleNamespace(storage=storage, bigquery=bigquery)


# ── In-memory GCS (models google-cloud-storage 3.14) ─────────────────────────
# The exit behaviour of blob.open() matters: a fake that did not model it hid
# a real bug (docs/issue.md #12).
#   "wb" → BlobWriter: an exception in the `with` block terminates the upload
#   "wt" → TextIOWrapper: close() finalizes the upload even on an exception

class _BlobWriter(io.BytesIO):
    def __init__(self, blob):
        super().__init__()
        self._blob = blob

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._blob.bucket.store[self._blob.name] = self.getvalue()
        super().close()
        return False


class _TextWriter(io.StringIO):
    def __init__(self, blob):
        super().__init__()
        self._blob = blob

    def __exit__(self, exc_type, exc, tb):
        self._blob.bucket.store[self._blob.name] = self.getvalue().encode("utf-8")
        super().close()
        return False


class FakeBlob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name
        self.metadata = bucket.meta.get(name)

    @property
    def size(self):
        return len(self.bucket.store[self.name])

    def exists(self):
        return self.name in self.bucket.store

    def upload_from_filename(self, path, **kw):
        self.bucket.store[self.name] = Path(path).read_bytes()

    def upload_from_string(self, data, content_type=None, **kw):
        self.bucket.store[self.name] = data.encode("utf-8") if isinstance(data, str) else data

    def download_as_bytes(self):
        if self.name not in self.bucket.store:
            raise FileNotFoundError(self.name)
        return self.bucket.store[self.name]

    def download_as_text(self):
        return self.download_as_bytes().decode("utf-8")

    def delete(self):
        if self.name not in self.bucket.store:
            raise FileNotFoundError(self.name)
        del self.bucket.store[self.name]
        self.bucket.meta.pop(self.name, None)

    def patch(self):
        self.bucket.meta[self.name] = dict(self.metadata or {})

    def open(self, mode="rb", **kw):
        if mode == "rb":
            return io.BytesIO(self.download_as_bytes())
        if mode == "wb":
            return _BlobWriter(self)
        if mode == "wt":
            return _TextWriter(self)
        raise ValueError(f"FakeBlob.open: unsupported mode {mode!r}")


class FakeBucket:
    """In-memory bucket: `store` maps object name → bytes; `meta` holds metadata."""

    def __init__(self, name="bkt"):
        self.name = name
        self.store: dict[str, bytes] = {}
        self.meta: dict[str, dict] = {}
        self.client = types.SimpleNamespace(list_blobs=self._list_blobs)

    def blob(self, name):
        return FakeBlob(self, name)

    def copy_blob(self, blob, destination_bucket, new_name):
        destination_bucket.store[new_name] = self.store[blob.name]
        return destination_bucket.blob(new_name)

    def rename_blob(self, blob, new_name):
        self.store[new_name] = self.store.pop(blob.name)
        return self.blob(new_name)

    def _list_blobs(self, bucket_name, prefix=None, max_results=None):
        names = sorted(n for n in self.store if n.startswith(prefix or ""))
        if max_results is not None:
            names = names[:max_results]
        return [self.blob(n) for n in names]


@pytest.fixture
def fake_bucket():
    return FakeBucket()


# ── Fake HTTP (requests.Session.get) ─────────────────────────────────────────

class FakeResponse:
    def __init__(self, content: bytes = b"", status_code: int = 200):
        self.content, self.status_code = content, status_code

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")

    def json(self):
        return json.loads(self.content)

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]


class FakeSession:
    """routes: {url substring: bytes | dict | (status, bytes)}. Records every call."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def get(self, url, **kw):
        self.calls.append(url)
        for part, body in self.routes.items():
            if part in url:
                status = 200
                if isinstance(body, tuple):
                    status, body = body
                if isinstance(body, (dict, list)):
                    body = json.dumps(body).encode("utf-8")
                return FakeResponse(body, status)
        return FakeResponse(b"not found", 404)


@pytest.fixture
def fake_session():
    return FakeSession


# ── Zip builders ─────────────────────────────────────────────────────────────

def build_openfda_zip(records, member="device-x-0001-of-0001.json", meta=None) -> bytes:
    """An openFDA bulk zip: one JSON member {"meta": …, "results": records}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, json.dumps({"meta": meta or {"last_updated": "2026-10-20"},
                                       "results": records}))
    return buf.getvalue()


def gudid_device_xml(key, device_id="D1", brand="Brand", company="ACME INC",
                     product_codes=("NOF",), extra=""):
    """One <device> element, in the shape of the real AccessGUDID bulk XML."""
    codes = "".join(f"<fdaProductCode><productCode>{c}</productCode>"
                    f"<productCodeName>n</productCodeName></fdaProductCode>"
                    for c in product_codes)
    return (
        '<device xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns="http://www.fda.gov/cdrh/gudid">'
        f"<publicDeviceRecordKey>{key}</publicDeviceRecordKey>"
        "<deviceRecordStatus>Published</deviceRecordStatus>"
        "<devicePublishDate>2018-07-06</devicePublishDate>"
        '<deviceCommDistributionEndDate xsi:nil="true"></deviceCommDistributionEndDate>'
        "<identifiers><identifier>"
        f"<deviceId>{device_id}</deviceId><deviceIdType>Primary</deviceIdType>"
        '<pkgQuantity xsi:nil="true"></pkgQuantity>'
        "</identifier></identifiers>"
        f"<brandName>{brand}</brandName><companyName>{company}</companyName>"
        f"<contacts/><productCodes>{codes}</productCodes>{extra}"
        "</device>")


def build_gudid_zip(parts, date="2026-10-01", per_part=None, db_total=None) -> bytes:
    """A GUDID bulk zip: parts = [[device_xml, …], …] → FULLDownload_Part{n}_Of_{N}_{date}.xml.

    per_part / db_total override the header counts (to test the count checks).
    """
    total = len(parts)
    db = db_total if db_total is not None else sum(len(p) for p in parts)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for n, devices in enumerate(parts, start=1):
            count = per_part if per_part is not None else len(devices)
            xml = (
                '<?xml version="1.0" encoding="UTF-8"?><gudid xmlns="http://www.fda.gov/cdrh/gudid" '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="1.0">\n'
                f'<header><database frequency="monthly" type="FULL">'
                f'<downloadFile part="{n}" totalParts="{total}"/>'
                f"<numberRecordXML>{count}</numberRecordXML>"
                f"<numberRecordsDatabase>{db}</numberRecordsDatabase>"
                "</database></header>\n" + "\n".join(devices) + "\n</gudid>")
            z.writestr(f"FULLDownload_Part{n}_Of_{total}_{date}.xml", xml)
    return buf.getvalue()


@pytest.fixture
def make_openfda_zip():
    return build_openfda_zip


@pytest.fixture
def make_gudid_zip():
    return build_gudid_zip


@pytest.fixture
def gudid_device():
    return gudid_device_xml
