"""Synthetic-only checks for public entrypoints, persisted roots and layout safety."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time

import pandas as pd
import pytest

import fqdb
from fqdb import config
from fqdb.config import Config
from fqdb.service.client import Client
from fqdb.util import FQDBError
from test_contracts import setup_store


def stop(client):
    if client.endpoint():
        client.call("shutdown")
    end = time.monotonic() + 15
    while client.endpoint() and time.monotonic() < end:
        time.sleep(0.1)


def test_root_precedence_and_saved_selection(data_root, monkeypatch):
    fake_home = data_root / "SYNTHETIC-home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("FQDB_DATA_DIR", raising=False)
    monkeypatch.delenv("FQD_DATA_DIR", raising=False)
    assert config.resolve_root() == fake_home / ".fqdb"
    saved, removed_env, new_env, initialized, explicit = [
        data_root / n for n in ("saved", "removed-setting", "new", "init", "explicit")
    ]
    config.select_root(saved)
    monkeypatch.setattr(config, "_current_root", None)
    assert config.resolve_root() == saved
    monkeypatch.setenv("FQD_DATA_DIR", str(removed_env))
    assert config.resolve_root() == saved  # Removed environment alias must not redirect data.
    monkeypatch.setenv("FQDB_DATA_DIR", str(new_env))
    assert config.resolve_root() == new_env
    config.select_root(initialized, persist=False)
    assert config.resolve_root() == initialized
    assert config.resolve_root(explicit) == explicit
    assert config.saved_root() == saved


def test_modern_layout_concurrent_initialization(data_root):
    root = data_root / "SYNTHETIC-new-root"
    with ThreadPoolExecutor(max_workers=6) as pool:
        configs = list(pool.map(lambda _: Config(root).ensure(), range(12)))
    assert all(c.layout == "canonical" for c in configs)
    assert (root / "catalog").is_dir() and (root / ".state/state").is_dir()
    assert not (root / "torrents").exists()
    assert not (root / "state").exists()
    assert not (root / "config.json").exists()
    assert (root / ".state/config.json").is_file()
    assert Config(root).read()["source_adapters"] == []


def test_dataset_queries_are_offline_and_share_selected_root(data_root):
    setup_store(data_root)
    fqdb.init(data_root)
    assert Config().root == data_root
    fqdb.query.register_alias("SYNTHETIC_bars", "cn/stock/base", "1d")
    aliased = fqdb.query.load(
        "SYNTHETIC_bars",
        symbols=["000100.SZ"],
        start="2026-11-12",
        end="2026-11-13",
        fields=["close"],
    )
    full = fqdb.query.load("cn/stock/base", symbols="000100.SZ", fields=["close"])
    prices = fqdb.get_price("000100.SZ", "2026-11-12", "2026-11-13", fields=["close"])
    pd.testing.assert_frame_equal(aliased, prices)
    pd.testing.assert_frame_equal(full, prices)
    assert fqdb.query.list_datasets() == ["cn/stock/base"]
    assert fqdb.query.dataset_schema("SYNTHETIC_bars")["synthetic"]
    with pytest.raises(FQDBError, match="conflicts"):
        fqdb.load("SYNTHETIC_bars", frequency="1m")
    with pytest.raises(FQDBError, match="Unavailable"):
        fqdb.load("cn/stock/base", symbols="000100.SZ", fields=["unavailable"])
    with pytest.raises(FQDBError, match="no local records"):
        fqdb.load("cn/stock/base", symbols="999999.SZ")
    assert not (data_root / ".state/state/service.json").exists()


def test_selected_root_is_shared_when_implicit_startup_is_disabled(data_root):
    root = data_root / "SYNTHETIC-selected"
    result = subprocess.run(
        [sys.executable, "-m", "fqdb", "init", "--data-dir", str(root)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["data_dir"] == str(root)
    result = subprocess.run(
        [sys.executable, "-m", "fqdb", "status"], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["data_dir"] == str(root) and value["service_running"] is False
    script = "import fqdb; from fqdb.config import Config; assert Config().root.as_posix()==__import__('sys').argv[1]; assert fqdb.provider.status()['service_running'] is False"
    subprocess.run([sys.executable, "-c", script, str(root)], check=True, timeout=15)
    assert not (root / ".state/state/service.json").exists()


def test_stopped_provider_status_and_stop_are_passive(data_root):
    fqdb.init(data_root)
    c = Client(data_root)
    assert fqdb.provider.status()["service_running"] is False
    assert fqdb.provider.stop()["enabled"] is False
    assert c.endpoint() is None
    assert not (data_root / ".state/state/service.json").exists()
