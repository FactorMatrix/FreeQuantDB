"""Current contract only: obsolete formats and layout bridges are rejected."""

import importlib.util
import json
from pathlib import Path

import pytest

from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.layout import runtime_path
from fqdb.schema.base import Dataset
from fqdb.schema.registry import base_dataset
from fqdb.storage.metadata import Store
from fqdb.storage.query import get_price
from fqdb.util import FQDBError


def test_current_root_contains_no_layout_aliases(empty_data_root):
    data_root = empty_data_root
    config = Config(data_root).ensure()
    assert config.layout == "canonical"
    assert {p.name for p in data_root.iterdir()} == {"data", "catalog", ".state"}
    assert not any(p.is_symlink() for p in data_root.rglob("*"))
    assert config.path == data_root / ".state/config.json"
    assert runtime_path(data_root, "catalog") == data_root / "catalog"
    assert Store(data_root).path == data_root / ".state/state/index.sqlite3"


@pytest.mark.parametrize("old_path", ["config.json", "state", "staging"])
def test_obsolete_root_is_rejected_without_migration(empty_data_root, old_path):
    data_root = empty_data_root
    path = data_root / old_path
    if old_path.endswith(".json"):
        path.write_text("{}")
    else:
        path.mkdir()
    with pytest.raises(FQDBError, match="obsolete"):
        Config(data_root).ensure()
    assert path.exists()
    assert not (data_root / ".state").exists()


def test_obsolete_layout_marker_is_not_accepted(empty_data_root):
    data_root = empty_data_root
    state = data_root / ".state"
    state.mkdir()
    (state / "layout.json").write_text(json.dumps({"schema_version": 1, "mode": "canonical"}))
    with pytest.raises(FQDBError, match="Unsupported"):
        Config(data_root).ensure()


def test_obsolete_descriptor_and_package_are_rejected(data_root):
    descriptor = base_dataset().to_dict()
    with pytest.raises(FQDBError):
        Dataset(**(descriptor | {"id": "stock/base"}))
    with pytest.raises(FQDBError):
        Dataset(**(descriptor | {"format_version": 1}))
    with pytest.raises(FQDBError):
        Entry(
            dataset="cn/stock/base",
            frequency="1d",
            trading_date="2026-11-13",
            version=1,
            file="data/cn/stock/base/1d/2026/11/2026-11-13.v1.parquet",
            size=1,
            sha256="0" * 64,
            info_hash="0" * 40,
            timezone="Asia/Shanghai",
            source="SYNTHETIC",
            license="CC0",
            synthetic=True,
            published_at="2026-11-13",
            format_version=1,
        )


def test_removed_query_keywords_and_migration_modules_are_absent(data_root, capsys):
    Config(data_root).ensure()
    with pytest.raises(TypeError):
        get_price(Store(data_root), symbols=["000100.SZ"], market="cn")
    assert importlib.util.find_spec("fqdb.migration") is None
    assert importlib.util.find_spec("fqdb.market_migration") is None
    from fqdb.cli import main

    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "migrate" not in capsys.readouterr().out


def test_default_root_does_not_guess_a_retired_host_location(data_root, monkeypatch):
    import fqdb.config as config

    home = data_root / "home"
    old = home / "colorful-data/FreeQuantDB-data"
    old.mkdir(parents=True)
    (old / "config.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(config, "_current_root", None)
    monkeypatch.delenv("FQDB_DATA_DIR", raising=False)
    monkeypatch.setenv("FQDB_USER_CONFIG", str(data_root / "no-saved-root.json"))
    assert Config().root == home / ".fqdb"


def test_local_publish_does_not_steal_an_active_download(data_root):
    from fqdb.demo import market_frame, market_schema
    from fqdb.service.core import Owner

    owner = Owner(Config(data_root))
    try:
        schema = market_schema(days=["2026-11-13"], symbols=["000100.SZ"])
        owner.store.register(schema)
        owner.dispatch(
            "publish",
            {
                "dataset": schema.id,
                "frequency": "1d",
                "input_path": market_frame(days=["2026-11-13"], symbols=["000100.SZ"]),
            },
        )
        entry = owner.store.entries()[0]
        identity = owner.store.enqueue(entry)
        with pytest.raises(FQDBError, match="Pause or cancel"):
            owner.seeding.verify(entry)
        assert owner.store.task(identity)["status"] == "queued"
    finally:
        owner.close()
