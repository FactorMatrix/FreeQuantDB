"""Synthetic filesystem catalog and mirror checks without production seeds."""

from contextlib import contextmanager
from datetime import date
from functools import partial
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from fqdb.catalog import magnet
from fqdb.catalog.magnet import MagnetCatalog, dataset_path, package_path
from fqdb.util import FQDBError

SCOPE = "cn/stock/base/1d"
ANNUAL = package_path(SCOPE, "2005")
MONTHLY = package_path(SCOPE, "202601")
DAILY = package_path(SCOPE, "20261117")
DATASET = dataset_path(SCOPE)
DAY = "2005-01-04"
DATASET_BODY = {
    "schema_version": 5,
    "name": "Synthetic stock bars",
    "fields": ["instrument", "trading_date", "close"],
    "instrument_column": "instrument",
    "time_column": "trading_date",
}


def description(day, identity=None, **stats):
    body = {
        "start_date": day,
        "end_date": day,
        "rows": 2,
        "instruments": 2,
        "trading_days": 1,
        "size_bytes": 400,
    }
    if identity:
        body["magnet"] = "magnet:?xt=urn:btih:" + identity * 40
    return body | stats


def write(root, relative, body):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body), encoding="utf-8")


def install_mirror(root, packages, dataset=DATASET_BODY):
    write(root, DATASET, dataset)
    for path, body in packages.items():
        write(root, path, body)
    write(root, "index.json", {"schema_version": 5, "datasets": [DATASET], "packages": list(packages)})
    return root


@contextmanager
def http_mirror(root):
    requests = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_head(self):
            path = root / self.path.lstrip("/")
            self.etag = '"' + hashlib.sha256(path.read_bytes()).hexdigest() + '"' if path.is_file() else None
            current = self.headers.get("If-None-Match")
            requests.append((self.path, current))
            if self.etag and current == self.etag:
                self.send_response(304)
                self.end_headers()
                return None
            return super().send_head()

        def end_headers(self):
            if self.etag:
                self.send_header("ETag", self.etag)
            super().end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(root)))
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/index.json", requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_same_http_source_discovers_added_packages_and_offline_mirror(data_root):
    root = data_root / "synthetic-mirror"
    catalog = MagnetCatalog(data_root / "client")
    annual = {ANNUAL: description(DAY, "a")}
    install_mirror(root, annual)
    with http_mirror(root) as (address, requests):
        first = catalog.sync(address)
        assert set(first["added"]) == {DATASET, ANNUAL}
        assert [path for path, _ in requests] == ["/index.json", "/" + DATASET, "/" + ANNUAL]
        assert all(etag is None for _, etag in requests)
        requests.clear()
        monthly = description("2026-01-05", "b")
        daily = description("2026-11-17", "c")
        install_mirror(root, annual | {MONTHLY: monthly, DAILY: daily})
        updated = catalog.sync(address)
        assert updated["added"] == [MONTHLY, DAILY]
        assert {path for path, etag in requests if etag} == {"/index.json", "/" + DATASET, "/" + ANNUAL}
        assert [row["period"] for row in updated["packages"]] == ["2005", "202601", "20261117"]
    with pytest.raises(FQDBError, match="Cannot read catalog object index.json"):
        catalog.sync(address)
    assert [row["period"] for row in catalog.scan()["packages"]] == ["2005", "202601", "20261117"]
    assert MagnetCatalog(data_root / "fresh-mirror-client").sync(root)["packages"][0]["info_hash"] == "a" * 40


def test_manually_edited_and_deleted_objects_survive_official_updates(data_root):
    root = data_root / "synthetic-mirror"
    catalog = MagnetCatalog(data_root / "client")
    annual = {ANNUAL: description(DAY, "a")}
    monthly = {MONTHLY: description("2026-01-05", "b")}
    install_mirror(root, annual | monthly)
    catalog.sync(root)
    local_dataset = DATASET_BODY | {"name": "My manual description"}
    write(catalog.directory, DATASET, local_dataset)
    (catalog.directory / ANNUAL).unlink()
    data = catalog.root / "data" / MONTHLY.removesuffix(".json")
    data.parent.mkdir(parents=True)
    data.write_bytes(b"synthetic bytes preserved when official descriptor is retired")

    corrected = {ANNUAL: description(DAY, "d")}
    install_mirror(root, corrected)
    updated = catalog.sync(root)
    assert updated["datasets"][0]["name"] == local_dataset["name"]
    assert updated["hidden"] == [ANNUAL] and updated["removed"] == [MONTHLY]
    assert not updated["packages"] and data.exists()
    write(catalog.directory, ANNUAL, description(DAY, "a"))
    assert catalog.scan()["packages"][0]["info_hash"] == "a" * 40
    assert catalog.sync(root)["packages"][0]["info_hash"] == "d" * 40

    (catalog.directory / DATASET).unlink()
    install_mirror(root, corrected | monthly)
    hidden = catalog.sync(root)
    assert hidden["hidden"] == [DATASET] and not hidden["packages"] and not hidden["datasets"]
    assert not (catalog.directory / MONTHLY).exists()
    write(catalog.directory, DATASET, local_dataset)
    assert catalog.sync(root)["packages"][1]["period"] == "202601"


def test_failed_refresh_and_partial_disk_write_retain_last_success(data_root, monkeypatch):
    root = data_root / "synthetic-mirror"
    catalog = MagnetCatalog(data_root / "client")
    annual = description(DAY, "a")
    install_mirror(root, {ANNUAL: annual})
    catalog.sync(root)
    original_state = catalog.sync_state.read_bytes()
    revised = description(DAY, "b")
    install_mirror(root, {ANNUAL: revised, MONTHLY: description("2026-01-05", magnet="invalid")})
    with pytest.raises(FQDBError, match="202601.parquet.json: Invalid package magnet"):
        catalog.sync(root)
    assert catalog.sync_state.read_bytes() == original_state
    assert catalog.scan()["packages"][0]["info_hash"] == "a" * 40

    install_mirror(root, {ANNUAL: revised, MONTHLY: description("2026-01-05", "c")})
    real_set = magnet._set

    def fail_second_write(path, payload):
        if path.name == "202601.parquet.json" and payload is not None:
            raise OSError("synthetic interrupted write")
        real_set(path, payload)

    monkeypatch.setattr(magnet, "_set", fail_second_write)
    with pytest.raises(OSError, match="synthetic interrupted write"):
        catalog.sync(root)
    assert not catalog.journal.exists() and not (catalog.directory / MONTHLY).exists()
    assert catalog.sync_state.read_bytes() == original_state
    assert catalog.scan()["packages"][0]["info_hash"] == "a" * 40


def test_manual_import_inspects_actual_parquet_and_reports_specific_defects(data_root):
    root = data_root / "synthetic-manual-input"
    catalog = MagnetCatalog(data_root / "client")
    data = catalog.root / "data" / ANNUAL.removesuffix(".json")
    data.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "instrument": ["synthetic-a", "synthetic-b"],
                "trading_date": [date.fromisoformat(DAY)] * 2,
                "close": [1.0, 2.0],
            }
        ),
        data,
    )
    local = description(DAY, size_bytes=data.stat().st_size)
    write(root, DATASET, DATASET_BODY)
    write(root, ANNUAL, local)
    scanned = catalog.import_local(root / ANNUAL)
    assert set(scanned["imported"]) == {ANNUAL, DATASET}
    package = scanned["packages"][0]
    assert package["local_status"] == "available" and package["magnet_status"] == "missing"
    assert package["coverage_status"] == "verified" and package["observed"] == package["expected"]
    assert catalog.scan()["packages"][0]["coverage_status"] == "verified"
    write(catalog.directory, ANNUAL, local | {"magnet": "irrelevant", "rows": 3, "instruments": 3})
    write(catalog.directory, DATASET, DATASET_BODY | {"fields": ["instrument", "trading_date", "other"]})
    wrong = catalog.scan(deep=True, path=ANNUAL)["packages"][0]
    assert wrong["magnet_status"] == "invalid" and wrong["coverage_status"] == "mismatch"
    assert wrong["local_status"] == "invalid"
    assert {issue["code"] for issue in wrong["issues"]} == {"magnet", "rows", "fields", "instruments"}
    data.write_bytes(b"invalid parquet")
    corrupt = catalog.scan()["packages"][0]
    assert (
        corrupt["observed"]["size_bytes"] == len(b"invalid parquet") and corrupt["local_status"] == "invalid"
    )
    data.unlink()
    assert catalog.scan()["packages"][0]["local_status"] == "missing"


def test_manifest_rejects_unsafe_paths_and_supports_other_markets(data_root):
    root = data_root / "synthetic-mirror"
    catalog = MagnetCatalog(data_root / "client")
    package = description(DAY, "a")
    install_mirror(root, {ANNUAL: package})
    with pytest.raises(FQDBError, match="Noncanonical"):
        catalog.sync(install_mirror(root, {"cn/stock/base/1d/2004/2005.parquet.json": package}))
    assert not catalog.scan()["datasets"]
    explicit = "us/etf/base/1d"
    catalog.write_publication(explicit, DATASET_BODY, "2005", package)
    assert catalog.scan()["packages"][0]["market"] == "us"
