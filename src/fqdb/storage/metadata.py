from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

from filelock import FileLock

from ..layout import runtime_path
from ..catalog.models import Entry
from ..schema.base import Dataset
from ..util import FQDBError, now, atomic_json, safe_path
from ..publish.paths import (
    atomic_link,
    immutable_file,
    install_bt_snapshot,
    install_snapshot,
    journal_path,
)

_VERIFICATION_COLUMN = "CASE WHEN b.hash IS NULL THEN 'full' ELSE 'bt' END AS verification"
_REVISION_ORDER = "ABS(version),version DESC"


class Store:
    def __init__(self, root, *, check_available=None):
        self.root = root
        self._check_available = check_available
        self.path = runtime_path(root, "state") / "index.sqlite3"
        if self._check_available is not None:
            self._check_available()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS datasets(id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(hash TEXT PRIMARY KEY, dataset TEXT, frequency TEXT,
                day TEXT, version INTEGER, body TEXT NOT NULL,
                UNIQUE(dataset,frequency,day,version));
            CREATE TABLE IF NOT EXISTS active(dataset TEXT,frequency TEXT,day TEXT,hash TEXT,
                verified_at TEXT,mtime_ns INTEGER,size INTEGER,
                PRIMARY KEY(dataset,frequency,day));
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,body TEXT NOT NULL,status TEXT,
                reason TEXT,progress REAL DEFAULT 0,attempts INTEGER DEFAULT 0,
                updated_at TEXT,next_retry REAL DEFAULT 0,metrics TEXT DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,created_at TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS verified_versions(hash TEXT PRIMARY KEY,
                verified_at TEXT,mtime_ns INTEGER,size INTEGER);
            CREATE TABLE IF NOT EXISTS bt_verified_versions(hash TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,body TEXT,next_run REAL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS subscription_packages(id TEXT PRIMARY KEY,body TEXT NOT NULL);
            """)
        self._recover_market_switches()

    def _recover_market_switches(self):
        commits = self.root / ".state/commits"
        if not commits.exists():
            return
        with FileLock(runtime_path(self.root, "state") / "market-publish.lock", timeout=30):
            for path in commits.glob("*.json"):
                body = json.loads(path.read_text())
                candidate = Entry(**body["new"]).validate(self.root)
                with self.db() as c:
                    active = c.execute(
                        "SELECT hash FROM active WHERE dataset=? AND frequency=? AND day=?", candidate.key
                    ).fetchone()
                current = (
                    candidate
                    if active and active[0] == candidate.info_hash
                    else Entry(**body["old"])
                    if body.get("old")
                    else None
                )
                if current:
                    evidence = self._verified_version(current.info_hash)
                    verify_copy = not (
                        body.get("bt_transfer") or (evidence and evidence["verification"] == "bt")
                    )
                    atomic_link(
                        immutable_file(self.root, current),
                        safe_path(self.root, current.file),
                        verify_copy=verify_copy,
                    )
                elif not active:
                    safe_path(self.root, candidate.file).unlink(missing_ok=True)
                path.unlink()

    @contextmanager
    def db(self):
        if self._check_available is not None:
            self._check_available()
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def register(self, dataset):
        with self.db() as c:
            c.execute(
                "INSERT INTO datasets VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (dataset.id, json.dumps(dataset.to_dict())),
            )

    def register_default(self, dataset):
        """Install or refresh generated fields on an uncustomized registry entry."""
        current = dataset.to_dict()
        encoded = json.dumps(current)
        with self.db() as c:
            row = c.execute("SELECT body FROM datasets WHERE id=?", (dataset.id,)).fetchone()
            if row is None:
                c.execute("INSERT OR IGNORE INTO datasets VALUES(?,?)", (dataset.id, encoded))
                return
            saved = json.loads(row[0])
            derived = {"fields", "frequency_fields"}
            if saved != current and all(
                key in derived or saved.get(key) == current.get(key) for key in saved.keys() | current.keys()
            ):
                c.execute("UPDATE datasets SET body=? WHERE id=? AND body=?", (encoded, dataset.id, row[0]))

    def dataset(self, id, required=True):
        with self.db() as c:
            row = c.execute("SELECT body FROM datasets WHERE id=?", (id,)).fetchone()
        if row:
            return Dataset(**json.loads(row[0]))
        if required:
            raise FQDBError(f"Unregistered dataset: {id}")

    def datasets(self):
        with self.db() as c:
            return [json.loads(r[0]) for r in c.execute("SELECT body FROM datasets ORDER BY id")]

    @staticmethod
    def _merge_identity(previous, incoming):
        if previous.key != incoming.key or previous.file != incoming.file:
            raise FQDBError("Conflicting metadata for immutable torrent")
        if previous.size is not None and incoming.size is not None and previous.size != incoming.size:
            raise FQDBError("Conflicting metadata for immutable torrent")
        magnet = previous.magnet or incoming.magnet
        if previous.sparse and incoming.sparse:
            return replace(previous, size=previous.size or incoming.size, magnet=magnet)
        if previous.sparse:
            return replace(
                incoming,
                version=previous.version,
                published_version=incoming.published_version or incoming.version,
                magnet=magnet,
            )
        if incoming.sparse:
            return replace(previous, magnet=magnet)
        # A richer catalog can previously have supplied this hash under a
        # local revision allocated when its magnet was first seen.
        fields = {"magnet", "torrent", "version", "published_version"}
        old = {key: value for key, value in previous.to_dict().items() if key not in fields}
        new = {key: value for key, value in incoming.to_dict().items() if key not in fields}
        if json.dumps(old, sort_keys=True) != json.dumps(new, sort_keys=True):
            raise FQDBError("Conflicting metadata for immutable torrent")
        return replace(previous, magnet=magnet)

    def _resolve_entry(self, connection, entry):
        if not entry.sparse:
            published_version = entry.published_version or entry.version
            rows = connection.execute(
                "SELECT body FROM entries WHERE dataset=? AND frequency=? AND day=? "
                "AND (version=? OR version<0) AND hash<>?",
                (*entry.key, published_version, entry.info_hash),
            )
            for row in rows:
                other = Entry(**json.loads(row[0]))
                if not other.sparse and (other.published_version or other.version) == published_version:
                    raise FQDBError("An immutable catalog version has conflicting hashes")
        existing = connection.execute("SELECT body FROM entries WHERE hash=?", (entry.info_hash,)).fetchone()
        if existing:
            return self._merge_identity(Entry(**json.loads(existing[0])), entry)
        if entry.sparse:
            latest = connection.execute(
                "SELECT MAX(ABS(version)) FROM entries WHERE dataset=? AND frequency=? AND day=?", entry.key
            ).fetchone()[0]
            return replace(entry, version=(latest or 0) + 1)
        return entry

    def validate_entry(self, entry):
        """Check the immutable identity and resolve its local revision without writing."""
        entry.validate(self.root)
        with self.db() as connection:
            return self._resolve_entry(connection, entry)

    def _write_entry(self, connection, entry):
        entry = self._resolve_entry(connection, entry)
        # Negative storage revisions reserve slots for catalog magnets.
        # Versioned local publications keep their original positive slot.
        version, magnet_ranked = entry.revision_order
        stored_version = -version if magnet_ranked else version
        connection.execute(
            "INSERT INTO entries VALUES(?,?,?,?,?,?) ON CONFLICT(hash) DO UPDATE SET "
            "version=excluded.version, body=excluded.body",
            (entry.info_hash, *entry.key, stored_version, json.dumps(entry.to_dict())),
        )
        return entry

    def add_entry(self, entry):
        entry.validate(self.root)
        with self.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._write_entry(connection, entry)

    def add_magnet(self, entry):
        if not entry.sparse:
            raise FQDBError("Expected a magnet-only package")
        return self.add_entry(entry)

    def entries(self, dataset=None, frequency=None, start_date=None, end_date=None, latest=True):
        sql, params = "SELECT body FROM entries WHERE 1=1", []
        for col, value in (("dataset", dataset), ("frequency", frequency)):
            if value:
                sql += f" AND {col}=?"
                params.append(value)
        sql += f" ORDER BY day,{_REVISION_ORDER}"
        with self.db() as c:
            entries = [Entry(**json.loads(r[0])) for r in c.execute(sql, params)]
        entries = [
            e
            for e in entries
            if (not start_date or e.end >= start_date) and (not end_date or e.start <= end_date)
        ]
        if latest:
            return sorted({e.key: e for e in entries}.values(), key=lambda e: (e.start, e.key))
        return entries

    def entries_by_hash(self, identities):
        hashes = list(dict.fromkeys(identities))
        entries = {}
        with self.db() as db:
            for start in range(0, len(hashes), 500):
                batch = hashes[start : start + 500]
                placeholders = ",".join("?" for _ in batch)
                for row in db.execute(f"SELECT hash,body FROM entries WHERE hash IN ({placeholders})", batch):
                    entries[row["hash"]] = Entry(**json.loads(row["body"]))
        return entries

    def stage_entry(self, entry, source):
        """Retain verified bytes under their immutable torrent identity before activation."""
        entry.validate(self.root)
        source = Path(source)
        return install_snapshot(self.root, entry, source)

    def activate(self, entry):
        from ..bt.torrents import torrent_cache_path, validate_daily
        from ..util import sha256

        with FileLock(runtime_path(self.root, "state") / "market-publish.lock", timeout=30):
            path = immutable_file(self.root, entry)
            if not path.is_file() or path.stat().st_size != entry.size or sha256(path) != entry.sha256:
                raise FQDBError("Validated immutable market snapshot is missing or changed")
            torrent = torrent_cache_path(self.root, entry)
            validate_daily(torrent.read_bytes(), entry, self.root)
            self._switch_active(entry, path)

    def activate_bt(self, entry, source, native_size):
        """Commit a native-complete single file and its already verified metadata."""
        from ..bt.torrents import torrent_cache_path, validate_daily

        source = Path(source)
        if source.name != Path(entry.file).name or source.is_symlink() or not source.is_file():
            raise FQDBError("Native torrent has not completed the expected local file")
        before = source.stat()
        if type(native_size) is not int or native_size <= 0 or before.st_size != native_size:
            raise FQDBError("Native torrent file size does not match the completed file")
        with FileLock(runtime_path(self.root, "state") / "market-publish.lock", timeout=30):
            entry = self.validate_entry(entry)
            torrent = torrent_cache_path(self.root, entry)
            info = validate_daily(torrent.read_bytes(), entry, self.root)
            if info.files().file_size(0) != native_size:
                raise FQDBError("Native torrent length disagrees with verified metadata")
            path = immutable_file(self.root, entry)
            evidence = self._verified_version(entry.info_hash)
            snapshot = path.stat() if path.is_file() else None
            unchanged = bool(
                snapshot is not None
                and evidence
                and snapshot.st_size == native_size == evidence["size"]
                and snapshot.st_mtime_ns == evidence["mtime_ns"]
            )
            if not unchanged:
                path = install_bt_snapshot(self.root, entry, source)
            after = source.stat()
            if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns) != (
                before.st_ino,
                before.st_dev,
                before.st_size,
                before.st_mtime_ns,
            ) or path.stat().st_size != native_size:
                raise FQDBError("Native torrent payload changed while activating")
            entry = self.add_entry(replace(entry, size=native_size))
            verification = evidence["verification"] if unchanged else "bt"
            self._switch_active(entry, path, bt_source=source, verification=verification)
        return entry

    def _switch_active(self, entry, path, *, bt_source=None, verification="full"):
        from ..util import sha256

        previous = self.active_revision(entry.key)
        old = previous[0] if previous else None
        if old and old.revision_order > entry.revision_order:
            self._record_verification(entry, path.stat(), verification)
            return
        visible = safe_path(self.root, entry.file)
        if old is None and visible.exists():
            if bt_source is None:
                trusted = sha256(visible) == entry.sha256
            else:
                trusted = visible.samefile(path) or visible.samefile(bt_source)
            if not trusted:
                if bt_source is not None:
                    raise FQDBError(
                        "unregistered_local_file: move the existing local file or verify it before downloading"
                    )
                raise FQDBError("Unregistered canonical file exists; clear obsolete data before publishing")
        journal = journal_path(self.root, entry)
        atomic_json(
            journal,
            {
                "old": old.to_dict() if old else None,
                "new": entry.to_dict(),
                "bt_transfer": bt_source is not None,
            },
        )
        try:
            atomic_link(path, visible, verify_copy=bt_source is None)
            self._activate_record(entry, visible.stat(), verification)
        except BaseException:
            if old:
                atomic_link(immutable_file(self.root, old), visible, verify_copy=bt_source is None)
            else:
                visible.unlink(missing_ok=True)
            journal.unlink(missing_ok=True)
            raise
        journal.unlink(missing_ok=True)

    def rollback_market_entry(self, entry, previous=None, *, keep_entry=False):
        """Restore the former active market snapshot after publication failure."""
        with FileLock(runtime_path(self.root, "state") / "market-publish.lock", timeout=30):
            with self.db() as c:
                row = c.execute(
                    "SELECT hash FROM active WHERE dataset=? AND frequency=? AND day=?", entry.key
                ).fetchone()
            if row and row[0] not in (entry.info_hash, previous.info_hash if previous else None):
                raise FQDBError("Publication advanced concurrently; cannot roll it back")
            if row and row[0] == entry.info_hash:
                visible = safe_path(self.root, entry.file)
                if previous:
                    atomic_link(immutable_file(self.root, previous), visible)
                else:
                    visible.unlink(missing_ok=True)
                with self.db() as c:
                    c.execute("DELETE FROM active WHERE dataset=? AND frequency=? AND day=?", entry.key)
                    if previous:
                        stat = visible.stat()
                        c.execute(
                            "INSERT INTO active VALUES(?,?,?,?,?,?,?)",
                            (*previous.key, previous.info_hash, now(), stat.st_mtime_ns, stat.st_size),
                        )
            with self.db() as c:
                c.execute("DELETE FROM verified_versions WHERE hash=?", (entry.info_hash,))
                c.execute("DELETE FROM bt_verified_versions WHERE hash=?", (entry.info_hash,))
                if not keep_entry:
                    c.execute(
                        "DELETE FROM entries WHERE hash=? AND NOT EXISTS(SELECT 1 FROM active WHERE hash=?)",
                        (entry.info_hash, entry.info_hash),
                    )

    @staticmethod
    def _write_verification(connection, entry, stat, verification):
        connection.execute(
            "INSERT INTO verified_versions VALUES(?,?,?,?) ON CONFLICT(hash) DO UPDATE SET "
            "verified_at=excluded.verified_at,mtime_ns=excluded.mtime_ns,size=excluded.size",
            (entry.info_hash, now(), stat.st_mtime_ns, stat.st_size),
        )
        if verification == "bt":
            connection.execute("INSERT OR IGNORE INTO bt_verified_versions VALUES(?)", (entry.info_hash,))
        else:
            connection.execute("DELETE FROM bt_verified_versions WHERE hash=?", (entry.info_hash,))

    def _record_verification(self, entry, stat, verification):
        with self.db() as c:
            self._write_verification(c, entry, stat, verification)

    def _activate_record(self, entry, stat, verification):
        with self.db() as c:
            self._write_verification(c, entry, stat, verification)
            current = c.execute(
                "SELECT e.body FROM active a JOIN entries e ON e.hash=a.hash "
                "WHERE a.dataset=? AND a.frequency=? AND a.day=?",
                entry.key,
            ).fetchone()
            if current and Entry(**json.loads(current[0])).revision_order > entry.revision_order:
                return
            c.execute(
                "INSERT INTO active VALUES(?,?,?,?,?,?,?) ON CONFLICT(dataset,frequency,day) "
                "DO UPDATE SET hash=excluded.hash,verified_at=excluded.verified_at,"
                "mtime_ns=excluded.mtime_ns,size=excluded.size",
                (*entry.key, entry.info_hash, now(), stat.st_mtime_ns, stat.st_size),
            )

    def aggregate_stale(self, entry):
        if entry.sparse or entry.period_kind == "day":
            return False
        from ..catalog.models import period_bounds

        first, last = period_bounds(entry.period_kind, entry.period)
        for day_entry in self.entries(entry.dataset, entry.frequency, first, last):
            if day_entry.period_kind == "day" and not day_entry.sparse:
                d = day_entry.trading_date
                if d not in entry.daily_sources or day_entry.daily_sources.get(d) != entry.daily_sources[d]:
                    return True
        return False

    def verified_versions(self):
        with self.db() as c:
            return {
                r["hash"]: dict(r)
                for r in c.execute(
                    f"SELECT v.*,{_VERIFICATION_COLUMN} FROM verified_versions v "
                    "LEFT JOIN bt_verified_versions b ON b.hash=v.hash"
                )
            }

    def _verified_version(self, identity):
        with self.db() as c:
            row = c.execute(
                f"SELECT v.*,{_VERIFICATION_COLUMN} FROM verified_versions v "
                "LEFT JOIN bt_verified_versions b ON b.hash=v.hash WHERE v.hash=?",
                (identity,),
            ).fetchone()
        return dict(row) if row else None

    def active(self, dataset=None, frequency=None, start_date=None, end_date=None):
        sql = (
            f"SELECT e.body,a.verified_at,a.mtime_ns,a.size,{_VERIFICATION_COLUMN} "
            "FROM active a JOIN entries e ON e.hash=a.hash "
            "LEFT JOIN bt_verified_versions b ON b.hash=a.hash"
        )
        predicates, parameters = [], []
        if dataset:
            predicates.append("a.dataset=?")
            parameters.append(dataset)
        if frequency:
            predicates.append("a.frequency=?")
            parameters.append(frequency)
        if predicates:
            sql += " WHERE " + " AND ".join(predicates)
        with self.db() as c:
            rows = c.execute(sql + " ORDER BY a.day", parameters).fetchall()
        result = []
        for r in rows:
            e = Entry(**json.loads(r[0]))
            if (not start_date or e.end >= start_date) and (not end_date or e.start <= end_date):
                result.append((e, dict(r)))
        return result

    def active_file_evidence(self, scope=None):
        """Read current file identities and stat evidence without expanding entries."""
        sql = (
            "SELECT json_extract(e.body, '$.file') AS file,a.hash,a.size,a.mtime_ns "
            "FROM active a JOIN entries e ON e.hash=a.hash"
        )
        if scope is not None:
            sql += " WHERE a.dataset=? AND a.frequency=?"
        with self.db() as c:
            return {
                row["file"]: (row["hash"], row["size"], row["mtime_ns"])
                for row in c.execute(sql, scope or ())
            }

    def revisions(self, key):
        with self.db() as c:
            rows = c.execute(
                f"SELECT body FROM entries WHERE dataset=? AND frequency=? AND day=? "
                f"ORDER BY {_REVISION_ORDER}",
                key,
            )
            return [Entry(**json.loads(row[0])) for row in rows]

    def active_revision(self, key):
        with self.db() as c:
            row = c.execute(
                f"SELECT e.body,a.verified_at,a.mtime_ns,a.size,{_VERIFICATION_COLUMN} FROM active a "
                "JOIN entries e ON e.hash=a.hash LEFT JOIN bt_verified_versions b ON b.hash=a.hash "
                "WHERE a.dataset=? AND a.frequency=? AND a.day=?",
                key,
            ).fetchone()
        return (Entry(**json.loads(row[0])), dict(row)) if row is not None else None

    def enqueue(self, entry, force=False, kind="day"):
        body = {"kind": kind, "entry": entry.to_dict() if isinstance(entry, Entry) else entry}
        identity = entry.info_hash if isinstance(entry, Entry) else entry["info_hash"]
        with self.db() as c:
            c.execute(
                "INSERT OR IGNORE INTO tasks(id,body,status,updated_at) VALUES(?,?,'queued',?)",
                (identity, json.dumps(body), now()),
            )
            if force:
                c.execute(
                    "UPDATE tasks SET status='queued',reason=NULL,progress=0,attempts=0,next_retry=0,"
                    "updated_at=? WHERE id=?",
                    (now(), identity),
                )
        return identity

    def enqueue_subscription(self, entries, subscription_id):
        """Create only unseen tasks, preserving every existing task decision."""
        if not entries:
            return []
        inserted = []
        with self.db() as c:
            c.execute("BEGIN IMMEDIATE")
            for entry in entries:
                identity = entry.info_hash
                if c.execute("SELECT 1 FROM tasks WHERE id=?", (identity,)).fetchone():
                    continue
                entry = self._write_entry(c, entry)
                body = {
                    "kind": "day",
                    "entry": entry.to_dict(),
                    "origin": "subscription",
                    "subscription_id": subscription_id,
                }
                c.execute(
                    "INSERT INTO tasks(id,body,status,updated_at) VALUES(?,?,'queued',?)",
                    (identity, json.dumps(body), now()),
                )
                inserted.append(identity)
        return inserted

    def subscription_tasks(self, subscription_id, statuses):
        placeholders = ",".join("?" for _ in statuses)
        with self.db() as c:
            return [
                self._task_record(row)
                for row in c.execute(
                    f"SELECT * FROM tasks WHERE status IN ({placeholders}) "
                    "AND json_extract(body,'$.subscription_id')=?",
                    (*statuses, subscription_id),
                )
            ]

    @staticmethod
    def _task_record(row):
        return dict(row) | {"body": json.loads(row["body"]), "metrics": json.loads(row["metrics"])}

    def tasks(self, *, newest_first=False):
        direction = "DESC" if newest_first else "ASC"
        with self.db() as c:
            rows = c.execute(f"SELECT * FROM tasks ORDER BY updated_at {direction},id").fetchall()
        return [self._task_record(row) for row in rows]

    def task(self, id):
        with self.db() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (id,)).fetchone()
        return self._task_record(row) if row is not None else None

    def set_task(self, id, **values):
        if not values.keys() <= {"status", "reason", "progress", "attempts", "next_retry", "metrics"}:
            raise FQDBError("Invalid task update")
        values["updated_at"] = now()
        if "metrics" in values:
            values["metrics"] = json.dumps(values["metrics"])
        with self.db() as c:
            c.execute(
                "UPDATE tasks SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), id),
            )

    def report(self, body):
        with self.db() as c:
            cur = c.execute("INSERT INTO reports(created_at,body) VALUES(?,?)", (now(), json.dumps(body)))
            return cur.lastrowid

    def reports(self):
        with self.db() as c:
            return [
                dict(r) | {"body": json.loads(r["body"])}
                for r in c.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 50")
            ]

    def subscribe(self, body, *, next_run=0, reset_selection=False):
        dataset = body.get("dataset")
        frequency = body.get("frequency")
        identity = f"{dataset}/{frequency}" if dataset else "catalog"
        if dataset and frequency not in self.dataset(dataset).frequencies:
            raise FQDBError(f"Frequency not declared in dataset: {frequency}")
        with self.db() as c:
            c.execute(
                "INSERT INTO subscriptions VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "body=excluded.body,next_run=excluded.next_run",
                (identity, json.dumps(body), next_run),
            )
            if reset_selection:
                c.execute("DELETE FROM subscription_packages WHERE id=?", (identity,))
        return identity

    def unsubscribe(self, identity):
        with self.db() as c:
            c.execute("DELETE FROM subscriptions WHERE id=?", (identity,))
            c.execute("DELETE FROM subscription_packages WHERE id=?", (identity,))

    def subscription(self, identity):
        with self.db() as c:
            row = c.execute("SELECT * FROM subscriptions WHERE id=?", (identity,)).fetchone()
        return dict(row) | {"body": json.loads(row["body"])} if row else None

    def subscriptions(self):
        with self.db() as c:
            return [
                dict(r) | {"body": json.loads(r["body"])} for r in c.execute("SELECT * FROM subscriptions")
            ]

    def subscription_packages(self, identity):
        with self.db() as c:
            row = c.execute("SELECT body FROM subscription_packages WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row is not None else None

    def save_subscription_packages(self, identity, packages):
        if packages is not None:
            with self.db() as c:
                c.execute(
                    "INSERT INTO subscription_packages VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                    (identity, json.dumps(packages, separators=(",", ":"))),
                )
