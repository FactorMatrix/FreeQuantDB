"""Synthetic catalog selection and task identity checks without BT traffic."""

import pytest

from fqdb.bt.torrents import torrent_cache_path
from fqdb.catalog.magnet_manifest import package_path
from fqdb.catalog.models import Entry
from fqdb.publish.paths import immutable_file
from fqdb.publish.publisher import Publisher, import_native_publication
from fqdb.service.core import Owner
from gateway_helpers import loopback_config
from test_contracts import DAYS, sample_frame, sample_schema
from test_magnet_catalog import SCOPE, description, install_mirror


DATASET, FREQUENCY = SCOPE.rsplit("/", 1)
SUBSCRIPTION = {"dataset": DATASET, "frequency": FREQUENCY, "download": True}


def test_latest_first_bootstrap_and_later_changes_from_manually_refreshed_catalog(data_root):
    owner = Owner(loopback_config(data_root / "SYNTHETIC-receiver"))
    catalog = owner.catalogs
    mirror = data_root / "SYNTHETIC-mirror"
    source = {"source": str(mirror)}
    annual = package_path(SCOPE, "2025")
    old_day = package_path(SCOPE, "20250103")
    latest = package_path(SCOPE, "20261113")
    following = package_path(SCOPE, "20261116")
    packages = {
        annual: description("2025-01-02", "a", end_date="2025-12-31"),
        old_day: description("2025-01-03", "b"),
        latest: description("2026-11-13", "c"),
    }
    try:
        install_mirror(mirror, packages)
        entries, observed = catalog.subscription_candidates(SUBSCRIPTION, catalog.refresh(source))
        assert [e.info_hash for e in entries] == ["c" * 40]
        assert catalog.enqueue_subscription(entries, SCOPE) == ["c" * 40]
        owner.store.save_subscription_packages(SCOPE, observed)

        # A refresh outside the scheduler can synchronize the mirror first.
        # Each subscription still sees the delta from its own observation.
        packages[latest] = description("2026-11-13", "d")
        packages[following] = description("2026-11-16", "e")
        install_mirror(mirror, packages)
        catalog.refresh(source)
        refreshed = catalog.refresh(source)
        assert refreshed["added"] == refreshed["updated"] == []
        entries, observed = catalog.subscription_candidates(SUBSCRIPTION, refreshed)
        assert [(e.info_hash, e.magnet) for e in entries] == [
            ("d" * 40, packages[latest]["magnet"]),
            ("e" * 40, packages[following]["magnet"]),
        ]
        assert catalog.enqueue_subscription(entries, SCOPE) == ["d" * 40, "e" * 40]
        owner.store.save_subscription_packages(SCOPE, observed)
        assert catalog.subscription_candidates(SUBSCRIPTION, refreshed) == ([], None)
        assert catalog.enqueue_subscription(entries, SCOPE) == []
    finally:
        owner.close()


def test_bounded_selection_prefers_canonical_archives_and_honors_granularity(data_root):
    owner = Owner(loopback_config(data_root / "SYNTHETIC-receiver"))
    mirror = data_root / "SYNTHETIC-mirror"
    source = {"source": str(mirror)}
    first, last = "2025-01-02", "2025-01-03"
    packages = {
        package_path(SCOPE, "2025"): description(first, "a", end_date=last, trading_days=2),
        package_path(SCOPE, "202501"): description(first, "b", end_date=last, trading_days=2),
        package_path(SCOPE, "20250102"): description(first, "c"),
        package_path(SCOPE, "20250103"): description(last, "d"),
    }
    try:
        install_mirror(mirror, packages)
        refreshed = owner.catalogs.refresh(source)
        scope = SUBSCRIPTION | {"start_date": first, "end_date": last}
        entries, _ = owner.catalogs.subscription_candidates(scope, refreshed)
        assert [(e.period, e.info_hash) for e in entries] == [("2025", "a" * 40)]
        entries, _ = owner.catalogs.subscription_candidates(scope | {"granularity": "month"}, refreshed)
        assert [(e.period, e.info_hash) for e in entries] == [("202501", "b" * 40)]

        refreshed["packages"] = [p for p in refreshed["packages"] if p["period_kind"] == "day"]
        assert owner.catalogs.subscription_candidates(scope | {"granularity": "month"}, refreshed)[0] == []
        entries, _ = owner.catalogs.subscription_candidates(
            scope | {"granularity": "month", "allow_daily_fallback": True}, refreshed
        )
        assert [e.info_hash for e in entries] == ["c" * 40, "d" * 40]
    finally:
        owner.close()


@pytest.mark.parametrize("sparse", [False, True])
def test_retired_archive_proves_actual_local_dates_without_filling_gaps(data_root, sparse):
    producer = Owner(loopback_config(data_root / "SYNTHETIC-publisher"))
    receiver = Owner(loopback_config(data_root / "SYNTHETIC-receiver"))
    schema = sample_schema()
    try:
        producer.store.register(schema)
        publish = Publisher(producer.store)
        publish.publish(DATASET, FREQUENCY, sample_frame(days=[DAYS[0], DAYS[2]]))
        annual = Entry(**publish.aggregate(DATASET, FREQUENCY, "year", "2026")["entries"][0])
        payload = torrent_cache_path(producer.store.root, annual).read_bytes()
        path = immutable_file(producer.store.root, annual)
        if sparse:
            entry = receiver.store.add_magnet(
                Entry.from_magnet(DATASET, FREQUENCY, annual.period, annual.magnet)
            )
            metadata = torrent_cache_path(receiver.store.root, entry)
            metadata.parent.mkdir(parents=True, exist_ok=True)
            metadata.write_bytes(payload)
            receiver.store.activate_bt(entry, path, annual.size)
        else:
            import_native_publication(receiver.store, annual, path, payload)
            (receiver.store.root / "catalog" / package_path(SCOPE, annual.period)).unlink()

        mirror = data_root / "SYNTHETIC-mirror"
        packages = {package_path(SCOPE, d.replace("-", "")): description(d, c) for d, c in zip(DAYS, "abc")}
        install_mirror(mirror, packages)
        refreshed = receiver.catalogs.refresh({"source": str(mirror)})
        assert {p["period_kind"] for p in refreshed["packages"]} == {"day"}
        scope = SUBSCRIPTION | {"start_date": DAYS[0], "end_date": DAYS[2]}
        entries, _ = receiver.catalogs.subscription_candidates(scope, refreshed)
        assert [e.trading_date for e in entries] == [DAYS[1]]
    finally:
        producer.close()
        receiver.close()


def test_only_pending_automatic_tasks_are_reauthorized_after_restart(data_root):
    config = loopback_config(data_root / "SYNTHETIC-receiver")
    owner = Owner(config)
    mirror = data_root / "SYNTHETIC-mirror"
    packages = {package_path(SCOPE, d.replace("-", "")): description(d, c) for d, c in zip(DAYS, "abc")}
    try:
        install_mirror(mirror, packages)
        scope = SUBSCRIPTION | {"start_date": DAYS[0], "end_date": DAYS[2]}
        entries, _ = owner.catalogs.subscription_candidates(
            scope, owner.catalogs.refresh({"source": str(mirror)})
        )
        owner.store.enqueue(entries[0])
        assert owner.catalogs.enqueue_subscription(entries, SCOPE) == ["b" * 40, "c" * 40]
        owner.transfers.control("b" * 40, "pause")
        owner.transfers.control("a" * 40, "cancel")
        owner.store.set_task("c" * 40, status="downloading")
        assert owner.catalogs.enqueue_subscription(entries, SCOPE) == []
    finally:
        owner.close()

    owner = Owner(config)
    try:
        owner.catalogs.resume_subscription(scope)
        assert {task["id"]: owner.transfers.task_state(task)["status"] for task in owner.store.tasks()} == {
            "a" * 40: "cancelled",
            "b" * 40: "paused",
            "c" * 40: "queued",
        }
        assert owner.store.task("c" * 40)["body"]["entry"]["magnet"] == entries[2].magnet
    finally:
        owner.close()
