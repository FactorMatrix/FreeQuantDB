"""Reject dishonest package coverage and honor explicit period-sharing scopes."""

from dataclasses import replace

import pytest

from fqdb.catalog.models import Entry
from fqdb.config import Config
from fqdb.demo import market_frame, market_schema
from fqdb.publish.publisher import Publisher
from fqdb.service.core import Owner
from fqdb.storage.metadata import Store
from fqdb.util import FQDBError
from fqdb.validation.files import validate_parquet


def prepared(data_root, periods=True):
    config = Config(data_root).ensure()
    store = Store(config.root)
    days = ["2026-11-13", "2026-11-17"]
    schema = market_schema("future", days=days, symbols=["RB2701.SHFE"])
    store.register(schema)
    frame = market_frame("future", "1m", days=days, symbols=["RB2701.SHFE"], night=True)
    Publisher(store).publish(schema.id, "1m", frame)
    if periods:
        Publisher(store).aggregate(schema.id, "1m", "month", "202611")
        Publisher(store).aggregate(schema.id, "1m", "year", "2026")
    return config, store, schema, days


def test_deep_validation_binds_day_source_digests_to_actual_parquet(data_root):
    _, store, schema, days = prepared(data_root)
    entry = next(e for e in store.entries() if e.period_kind == "month")
    path = store.root / entry.file
    assert validate_parquet(path, entry, schema) == 2
    false_sources = {**entry.daily_sources, days[0]: "0" * 64}
    dishonest = replace(entry, daily_sources=false_sources)
    dishonest.validate(store.root)
    with pytest.raises(FQDBError, match="source digest"):
        validate_parquet(path, dishonest, schema)


def test_swapped_future_night_row_group_index_is_rejected(data_root):
    _, store, schema, days = prepared(data_root)
    entry = next(e for e in store.entries() if e.period_kind == "month")
    swapped = replace(
        entry, day_groups={days[0]: entry.day_groups[days[1]], days[1]: entry.day_groups[days[0]]}
    )
    swapped.validate(store.root)
    with pytest.raises(FQDBError, match="source digest"):
        validate_parquet(store.root / entry.file, swapped, schema)


def test_provider_date_scope_excludes_indivisible_period_files_with_other_days(data_root):
    config, store, _, days = prepared(data_root)
    owner = Owner(config)
    try:
        entries = store.entries()
        assert not any(owner.seeding.wants_seed(e) for e in entries)
        owner.dispatch(
            "provider_start",
            {"datasets": ["cn/future/base/1m"], "start_date": days[0], "end_date": days[0]},
        )
        allowed = [e for e in entries if owner.seeding.wants_seed(e)]
        assert len(allowed) == 1 and allowed[0].period_kind == "day" and allowed[0].trading_date == days[0]
        config.update(provider_end=days[1])
        assert len([e for e in entries if owner.seeding.wants_seed(e)]) == 4
    finally:
        owner.close()


@pytest.mark.parametrize("daily_fallback", [False, True])
def test_local_catalog_plan_retains_requested_granularity_and_explicit_daily_fallback(
    data_root, daily_fallback
):
    config, store, schema, days = prepared(data_root, periods=not daily_fallback)
    owner = Owner(config)
    try:
        preview = owner.catalogs.plan(
            {
                "dataset": schema.id,
                "frequency": "1m",
                "start_date": days[0],
                "end_date": days[1],
                "granularity": "month",
                "allow_daily_fallback": daily_fallback,
            }
        )
        assert preview["expected_kind"] == "month"
        assert preview["daily_fallback_selected"] is daily_fallback
        assert len(preview["items"]) == (2 if daily_fallback else 1)
        assert {Entry(**item["entry"]).period_kind for item in preview["items"]} == (
            {"day"} if daily_fallback else {"month"}
        )
        assert not store.tasks(), (
            "All selected local packages are already verified; planning must not invent transfers"
        )
    finally:
        owner.close()


def test_explicit_repair_replaces_only_corrupt_current_snapshot(data_root):
    from fqdb.publish.paths import immutable_file

    _, store, _, _ = prepared(data_root, periods=False)
    entry = store.entries()[0]
    visible = data_root / entry.file
    history = immutable_file(data_root, entry)
    clean = data_root / "SYNTHETIC-verified-repair.parquet"
    clean.write_bytes(visible.read_bytes())
    visible.write_bytes(b"BROKEN SYNTHETIC SNAPSHOT")
    assert history.read_bytes() == visible.read_bytes()
    store.stage_entry(entry, clean)
    store.activate(entry)
    assert visible.read_bytes() == history.read_bytes() == clean.read_bytes()
    before = history.stat().st_mtime_ns
    store.stage_entry(entry, clean)
    assert history.stat().st_mtime_ns == before
    bad = data_root / "SYNTHETIC-bad-repair.parquet"
    bad.write_bytes(b"WRONG CONTENT")
    original = visible.read_bytes()
    with pytest.raises(FQDBError, match="trusted manifest"):
        store.stage_entry(entry, bad)
    assert visible.read_bytes() == history.read_bytes() == original
