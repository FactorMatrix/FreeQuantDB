"""Historical synthetic versions remain eligible only with fresh verification."""

import time

from fqdb.config import Config
from fqdb.bt.torrents import torrent_cache_path
from fqdb.publish.paths import immutable_file
from test_contracts import sample_frame, sample_schema
from fqdb.publish.publisher import Publisher
from fqdb.service.core import Owner
from fqdb.storage.metadata import Store
from fqdb.storage.query import get_price


def until(predicate):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Synthetic provider state did not converge")


def seeding(owner):
    return {r["id"] for r in owner.status()["transfers"] if r["seeding"]}


def test_historical_seed_evidence_invalidation_and_deep_revalidation(data_root):
    cfg = Config(data_root).ensure()
    store = Store(data_root)
    schema = sample_schema(days=["2026-11-12"])
    store.register(schema)
    source = data_root / ".state/imports/SYNTHETIC-versioned.csv"
    for version, correction in ((1, 0), (2, 1)):
        sample_frame(days=["2026-11-12"], correction=correction).to_csv(source, index=False)
        Publisher(store).publish(schema.id, "1d", source, version=version)
    first, second = store.entries(latest=False)
    assert set(store.verified_versions()) == {first.info_hash, second.info_hash}
    old_file, old_torrent = immutable_file(data_root, first), torrent_cache_path(data_root, first)
    original, torrent_bytes = old_file.read_bytes(), old_torrent.read_bytes()
    owner = Owner(cfg)
    scope = {"datasets": [f"{schema.id}/1d"], "seed_old_versions": True}
    owner.dispatch("provider_start", scope)
    owner.start()
    try:
        until(lambda: {first.info_hash, second.info_hash} <= seeding(owner))
        # Changing historical bytes invalidates its evidence; the latest verified
        # version continues serving and remains the active query pointer.
        old_file.write_bytes(b"BROKEN SYNTHETIC FILE" + original[21:])
        until(lambda: first.info_hash not in seeding(owner) and second.info_hash in seeding(owner))
        issues = owner.status()["provider_issues"]
        assert any(
            r["entry"]["info_hash"] == first.info_hash and r["reason"] == "local_file_changed_or_unverified"
            for r in issues
        )
        rejected = owner.dispatch("provider_start", scope)["provision"]
        assert rejected["eligible"] == 1 and first.info_hash not in seeding(owner)
        old_file.write_bytes(original)
        until(lambda: first.info_hash not in seeding(owner))
        recovered = owner.dispatch("provider_start", scope)["provision"]
        assert recovered["eligible"] == 2
        until(lambda: {first.info_hash, second.info_hash} <= seeding(owner))
        assert old_file.read_bytes() == original and old_torrent.read_bytes() == torrent_bytes
        assert store.active()[0][0].info_hash == second.info_hash
        assert get_price(store, "000100.SZ", "2026-11-12", "2026-11-12", fields=["close"]).iloc[0, 0] == 11.5
    finally:
        owner.close()
