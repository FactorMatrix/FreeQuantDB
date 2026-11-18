"""Prepare explicitly requested local sharing using private native metadata."""

from dataclasses import replace

from ..bt.torrents import create_metadata, torrent_cache_path, validate_daily, verify_pieces
from ..catalog.magnet import MagnetCatalog
from ..catalog.models import Entry
from ..publish.paths import current_hash, resolve_file
from ..schema.digest import contract_hash
from ..util import FQDBError, atomic_bytes, safe_path, sha256
from ..validation.files import validate_parquet


def selected(entry, settings):
    days = (entry.start, entry.end) if entry.sparse else entry.days
    return (
        not settings["provider_scopes"] or f"{entry.dataset}/{entry.frequency}" in settings["provider_scopes"]
    ) and all(
        (not settings["provider_start"] or d >= settings["provider_start"])
        and (not settings["provider_end"] or d <= settings["provider_end"])
        for d in days
    )


def _metadata(root, entry, path):
    """Rebuild absent metadata only if the file has the original swarm identity."""
    cached = torrent_cache_path(root, entry)
    if cached.exists():
        try:
            return validate_daily(cached.read_bytes(), entry, root), False
        except Exception as exc:
            raise FQDBError(f"invalid_torrent_metadata: {exc}") from exc
    identity, payload = create_metadata(path)
    if identity != entry.info_hash:
        raise FQDBError("missing_torrent_metadata: original info-hash cannot be reconstructed")
    info = validate_daily(payload, entry, root)
    try:
        atomic_bytes(cached, payload, immutable=True)
    except FileExistsError:
        info = validate_daily(cached.read_bytes(), entry, root)
    return info, True


def _fresh(path, snapshot, entry, evidence):
    if not evidence or not snapshot.is_file():
        return False
    before = path.stat()
    original = snapshot.stat()
    return (
        before.st_size == original.st_size == evidence["size"] == entry.size
        and before.st_mtime_ns == original.st_mtime_ns == evidence["mtime_ns"]
    )


def _prepare_rich(store, entry, path, fresh, verify_seed):
    schema = store.dataset(entry.dataset)
    if not schema.units_confirmed:
        raise FQDBError("pending_units")
    if (
        entry.timezone != schema.timezone
        or entry.source != schema.source
        or entry.license != schema.license
        or entry.synthetic != schema.synthetic
        or (entry.schema_hash and entry.schema_hash != contract_hash(schema))
    ):
        raise FQDBError("data_invalid: manifest provenance differs from the dataset description")
    before = path.stat()
    if not fresh:
        if before.st_size != entry.size or sha256(path) != entry.sha256:
            raise FQDBError("data_invalid: local bytes differ from the trusted publication")
        validate_parquet(path, entry, schema)
    info, rebuilt = _metadata(store.root, entry, path)
    if (not fresh or rebuilt) and not verify_pieces(info, path):
        raise FQDBError("data_invalid: local BT piece hashes differ")
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise FQDBError("file_changed_during_check")
    if not fresh or current_hash(store.root, entry) != entry.info_hash:
        store.stage_entry(entry, path)
        if not fresh:
            verify_seed(entry)
        store.activate(entry)
    return rebuilt


def prepare(store, verify_seed, settings, *, cancelled):
    root = store.root
    issues, prepared, checked = [], 0, 0
    cached = store.verified_versions()

    def issue(reason, *, entry=None, file=None, detail=""):
        item = {"reason": reason}
        if entry is not None:
            item["entry"] = entry.to_dict()
        if file is not None:
            item["file"] = file
        if detail:
            item["detail"] = detail
        issues.append(item)

    catalog = MagnetCatalog(root).scan()
    candidates = {e.info_hash: e for e in store.entries(latest=settings["seed_old_versions"] is False)}
    if not settings["seed_old_versions"]:
        candidates.update({e.info_hash: e for e, _ in store.active()})
    for package in catalog["packages"]:
        if cancelled():
            break
        if not package["info_hash"] or package["info_hash"] in candidates:
            continue
        if settings["provider_scopes"] and package["scope"] not in settings["provider_scopes"]:
            continue
        if not safe_path(root, package["file"]).is_file():
            continue
        try:
            entry = Entry.from_magnet(
                package["dataset"], package["frequency"], package["period"], package["magnet"]
            )
            if selected(entry, settings):
                entry = store.add_magnet(replace(entry, size=package["size_bytes"]))
                candidates[entry.info_hash] = entry
        except FQDBError as exc:
            issue("catalog_invalid", file=package["file"], detail=str(exc))

    ready = set()
    for entry in candidates.values():
        if cancelled():
            break
        if not selected(entry, settings):
            continue
        checked += 1
        task = store.task(entry.info_hash)
        if task and task["status"] not in ("complete", "failed", "cancelled", "paused"):
            issue("active_download", entry=entry)
            continue
        path = resolve_file(root, entry)
        if not path.is_file() and not current_hash(root, entry):
            path = safe_path(root, entry.file)
        if not path.is_file():
            issue("published_but_local_missing", entry=entry)
            continue
        try:
            evidence = cached.get(entry.info_hash)
            fresh = _fresh(path, resolve_file(root, entry, immutable=True), entry, evidence)
            if entry.sparse:
                info, rebuilt = _metadata(root, entry, path)
                if not fresh:
                    if not verify_pieces(info, path):
                        raise FQDBError("data_invalid: local BT piece hashes differ")
                    store.activate_bt(entry, path, info.files().file_size(0))
            else:
                rebuilt = _prepare_rich(
                    store, entry, path, fresh and evidence["verification"] == "full", verify_seed
                )
            ready.add(entry.info_hash)
            prepared += int(rebuilt)
        except Exception as exc:
            reason = str(exc).split(":", 1)[0]
            if reason not in {
                "pending_units",
                "missing_torrent_metadata",
                "invalid_torrent_metadata",
                "file_changed_during_check",
            }:
                reason = "data_invalid"
            issue(reason, entry=entry, detail=str(exc))
    local = {e.info_hash for e, _ in store.active()}
    eligible = ready if settings["seed_old_versions"] else ready & local
    if eligible:
        status, message = "waiting_peers", f"已开启做种，可提供 {len(eligible)} 个数据包，等待连接。"
    elif any(i["reason"] == "published_but_local_missing" for i in issues):
        status, message = "missing_files", "本地数据文件缺失，无法开始做种。"
    elif any(
        i["reason"] in {"data_invalid", "invalid_torrent_metadata", "file_changed_during_check"}
        for i in issues
    ):
        status, message = "invalid_data", "本地数据未通过校验，无法开始做种。"
    elif issues:
        status, message = "pending_metadata", "本地数据缺少必要元数据，暂时无法做种。"
    else:
        status, message = "no_data", "暂无可提供的本地数据。"
    return {
        "status": status,
        "message": message,
        "eligible": len(eligible),
        "checked": checked,
        "prepared": prepared,
        "issues": issues,
    }
