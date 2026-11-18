"""Publish and aggregate market packages for a single local store."""

from dataclasses import replace
from pathlib import Path
import shutil
import tempfile

from filelock import FileLock
import pyarrow.parquet as pq

from ..bt.torrents import create_metadata, make_magnet, torrent_cache_path, validate_daily, verify_pieces
from ..catalog.models import Entry, market_relative, period_bounds
from ..layout import runtime_path
from ..schema.digest import contract_hash, logical_digest
from ..util import FQDBError, atomic_bytes, now, sha256
from ..validation.quality import validate
from .catalog import write_publication
from .coverage import confirm_coverage
from .input import frames_by_day
from .paths import immutable_file, install_snapshot, resolve_file
from .spool import ParquetSpool


class Publisher:
    """Own an immutable publication and the switch to its active revision."""

    def __init__(self, store, *, verify_seed=None):
        self.store = store
        self.verify_seed = verify_seed

    def publish(
        self,
        dataset,
        frequency,
        input_path,
        version=1,
        progress=None,
        *,
        granularity="day",
        period=None,
        trading_dates=None,
        complete=False,
        as_of=None,
    ):
        """Publish local rows with explicit per-row or per-group minute trading dates."""
        original = self.store.dataset(dataset)
        if granularity not in ("day", "month", "year") or type(complete) is not bool:
            raise FQDBError("Invalid publication granularity/completion flag")
        schema = original.for_frequency(frequency)
        frames = frames_by_day(input_path, schema, frequency, trading_dates, period)
        entries = []
        if granularity == "day":
            for trading_day, frame in frames:
                label = trading_day.replace("-", "")
                if period is not None and label != period:
                    raise FQDBError("Source trading day differs from requested daily period")
                entries.append(
                    self._write_package(
                        original,
                        schema,
                        [(trading_day, frame)],
                        "day",
                        label,
                        version=version,
                        progress=progress,
                        complete=complete,
                        as_of=as_of,
                    )
                )
        else:
            if not period:
                raise FQDBError("Month/year publication requires an explicit YYYYMM/ YYYY period")
            entries.append(
                self._write_package(
                    original,
                    schema,
                    frames,
                    granularity,
                    str(period),
                    version=version,
                    progress=progress,
                    complete=complete,
                    as_of=as_of,
                )
            )
        if not entries:
            raise FQDBError("No market records to publish")
        return {"entries": entries, "synthetic": original.synthetic, "external_publication": False}

    def aggregate(
        self, dataset, frequency, granularity, period, *, progress=None, complete=False, as_of=None
    ):
        """Stream verified daily packages into an independent month or year package."""
        first, last = period_bounds(granularity, str(period))
        if granularity not in ("month", "year"):
            raise FQDBError("Aggregation requires month or year granularity")
        original = self.store.dataset(dataset)
        schema = original.for_frequency(frequency)
        result = self._write_package(
            original,
            schema,
            self._verified_days(original, schema, first, last),
            granularity,
            str(period),
            version=1,
            progress=progress,
            complete=complete,
            as_of=as_of,
        )
        return {"entries": [result], "synthetic": original.synthetic, "external_publication": False}

    def _verified_days(self, original, schema, first, last):
        dataset, frequency = schema.id, schema.active_frequency
        active = {
            entry.trading_date: (entry, cache)
            for entry, cache in self.store.active(dataset, frequency, first, last)
            if entry.period_kind == "day"
        }
        entries = [
            entry
            for entry in self.store.entries(dataset, frequency, first, last)
            if entry.period_kind == "day"
        ]
        if not entries:
            raise FQDBError("No verified daily market files available for aggregation")
        fingerprint = contract_hash(original)
        for entry in sorted(entries, key=lambda value: value.trading_date):
            trading_day = entry.trading_date
            pair = active.get(trading_day)
            if pair is None or pair[0].info_hash != entry.info_hash:
                raise FQDBError(f"Daily file is missing or stale: {trading_day}")
            path = resolve_file(self.store.root, entry)
            cache = pair[1]
            stat = path.stat()
            if (
                stat.st_mtime_ns != cache["mtime_ns"]
                or stat.st_size != cache["size"]
                or sha256(path) != entry.sha256
            ):
                raise FQDBError(f"Daily file changed before aggregation: {trading_day}")
            if entry.schema_hash != fingerprint:
                raise FQDBError("Daily source dataset contract changed")
            data = pq.read_table(path).to_pandas()
            data = validate(
                data,
                schema,
                trading_day,
                trading_dates=[trading_day] * len(data) if frequency == "1m" else None,
            )
            if logical_digest(data, schema) != entry.daily_sources[trading_day]:
                raise FQDBError("Daily source digest differs from the trusted manifest")
            yield trading_day, data

    def _write_package(self, original, schema, frames, kind, period, *, version, progress, complete, as_of):
        root = self.store.root
        frequency = schema.active_frequency
        relative = "data/" + market_relative(schema.id, frequency, kind, period)
        staging = runtime_path(root, "staging")
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=staging, prefix="market-publish-") as temporary:
            path = Path(temporary) / Path(relative).name
            content = ParquetSpool(schema).write(path, frames, progress)
            days = tuple(sorted(content.groups))
            as_of = confirm_coverage(original, frequency, days, kind, period, complete, as_of, content.counts)
            digest = sha256(path)
            key = schema.id, frequency, f"{kind}:{period}" if kind != "day" else days[0]
            previous = self.store.revisions(key)
            latest = previous[-1] if previous else None
            if (
                latest
                and latest.sha256 == digest
                and latest.daily_sources == content.digests
                and latest.complete == complete
                and latest.as_of == as_of
            ):
                return self._adopt_existing(latest, path)
            identity, payload = create_metadata(path)
            entry = Entry(
                dataset=schema.id,
                frequency=frequency,
                trading_date=days[0],
                version=max(int(version or 1), (latest.version if latest else 0) + 1),
                file=relative,
                size=path.stat().st_size,
                sha256=digest,
                info_hash=identity,
                timezone=schema.timezone,
                source=schema.source,
                license=schema.license,
                synthetic=schema.synthetic,
                published_at=now(),
                rows=content.rows,
                symbols=tuple(sorted(content.symbols)),
                schema_hash=contract_hash(original),
                format_version=2,
                period_kind=kind,
                period=period,
                start_date=days[0],
                end_date=days[-1],
                trading_days=days,
                day_groups=content.groups,
                daily_sources=content.digests,
                complete=complete,
                as_of=as_of,
            )
            entry = replace(entry, magnet=make_magnet(entry)).validate(root)
            self._stage(entry, path, payload)
            with self._publication_lock():
                # Another producer may have published while this process verified bytes.
                revisions = self.store.revisions(entry.key)
                same = next((item for item in revisions if item.info_hash == entry.info_hash), None)
                if same:
                    if same.sparse:
                        return self._activate(entry, register=True)
                    if same.daily_sources != entry.daily_sources:
                        raise FQDBError("Existing content identity has a conflicting trading-day index")
                    return same.to_dict()
                if revisions and entry.version <= revisions[-1].version:
                    entry = replace(entry, version=revisions[-1].version + 1)
                return self._activate(entry, register=True)

    def _adopt_existing(self, entry, path):
        root = self.store.root
        active = self.store.active_revision(entry.key)
        visible = root / entry.file
        snapshot = immutable_file(root, entry)
        if (
            active is not None
            and active[0].info_hash == entry.info_hash
            and visible.is_file()
            and visible.stat().st_size == active[1]["size"]
            and visible.stat().st_mtime_ns == active[1]["mtime_ns"]
            and snapshot.is_file()
            and sha256(snapshot) == entry.sha256
        ):
            write_publication(self.store, entry)
            return entry.to_dict()

        # A trusted catalog may know this identity before the bytes arrive.
        identity, payload = create_metadata(path)
        if identity != entry.info_hash:
            raise FQDBError("Known market content has a conflicting torrent identity")
        with self._publication_lock():
            self._stage(entry, path, payload)
            return self._activate(entry, register=False)

    def _publication_lock(self):
        return FileLock(runtime_path(self.store.root, "state") / "publication.lock", timeout=60)

    def _stage(self, entry, path, payload):
        root = self.store.root
        info = validate_daily(payload, entry, root)
        if not verify_pieces(info, path):
            raise FQDBError("Market file failed native torrent piece verification")
        install_snapshot(root, entry, path)
        cached = torrent_cache_path(root, entry)
        if cached.exists():
            validate_daily(cached.read_bytes(), entry, root)
        else:
            atomic_bytes(cached, payload, immutable=True)
        if self.verify_seed is not None:
            self.verify_seed(entry)

    def _activate(self, entry, *, register):
        active = self.store.active_revision(entry.key)
        known = self.store.entries_by_hash([entry.info_hash])
        try:
            if register:
                entry = self.store.add_entry(entry)
            self.store.activate(entry)
            current = self.store.active_revision(entry.key)
            if current is not None and current[0].info_hash == entry.info_hash:
                write_publication(self.store, entry)
        except BaseException:
            self.store.rollback_market_entry(
                entry, active[0] if active else None, keep_entry=entry.info_hash in known
            )
            raise
        return entry.to_dict()


def import_native_publication(store, entry, source, metadata):
    """Explicitly import verified legacy native metadata without changing its swarm."""
    source = Path(source)
    entry.validate(store.root)
    if entry.sparse or source.is_symlink() or not source.is_file() or source.name != Path(entry.file).name:
        raise FQDBError("Migration needs a rich publication and its original single Parquet file")
    info = validate_daily(metadata, entry, store.root)
    trackers = [tracker.url for tracker in info.trackers()]
    entry = replace(entry, magnet=make_magnet(entry, trackers), torrent="")
    publisher = Publisher(store)
    staging = runtime_path(store.root, "staging")
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging, prefix="native-import-") as directory:
        local = Path(directory) / source.name
        shutil.copy2(source, local)
        with publisher._publication_lock():
            publisher._stage(entry, local, metadata)
            return publisher._activate(entry, register=True)
