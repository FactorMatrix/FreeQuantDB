from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote_plus, urlparse

import libtorrent as lt

from ..util import FQDBError, atomic_bytes, safe_path


def info_hash(info):
    return str(info.info_hashes().v1)


def create_metadata(path, piece_size=256 * 1024):
    """Hash a local file and return its v1 identity and encoded metadata."""
    path = Path(path)
    files = lt.file_storage()
    files.add_file(path.name, path.stat().st_size)
    torrent = lt.create_torrent(files, piece_size, flags=lt.create_torrent.v1_only)
    torrent.set_creator("FreeQuantDB/0.1.0")
    lt.set_piece_hashes(torrent, str(path.parent))
    payload = lt.bencode(torrent.generate())
    return info_hash(lt.torrent_info(payload)), payload


def create(path, output, piece_size=256 * 1024):
    identity, payload = create_metadata(path, piece_size)
    atomic_bytes(Path(output), payload, immutable=True)
    return identity


MAX_METADATA = 16 * 1024 * 1024


def inspect(payload, root):
    if len(payload) > MAX_METADATA:
        raise FQDBError("Torrent metadata too large")
    info = lt.torrent_info(payload)
    fs = info.files()
    for i in range(fs.num_files()):
        safe_path(root, fs.file_path(i))
        if fs.file_flags(i) & lt.file_storage.flag_symlink:
            raise FQDBError("Symlinks in torrents are forbidden")
    return info


def validate_info(info, entry, root):
    raw = bytes(info.info_section())
    if len(raw) > MAX_METADATA:
        raise FQDBError("Torrent metadata too large")
    body = lt.bdecode(raw)
    if not isinstance(body, dict) or b"files" in body or b"file tree" in body or b"meta version" in body:
        raise FQDBError("Daily torrent must contain exactly one v1 file")
    try:
        name = body[b"name"].decode("utf-8", errors="strict")
    except (KeyError, AttributeError, UnicodeError) as exc:
        raise FQDBError("Invalid torrent filename encoding") from exc
    safe_path(root, name)
    if b"symlink path" in body or b"l" in body.get(b"attr", b""):
        raise FQDBError("Symlinks in torrents are forbidden")
    size = body.get(b"length")
    if type(size) is not int or size <= 0 or (entry.size is not None and size != entry.size):
        raise FQDBError("Torrent metadata does not match trusted daily manifest")
    piece_length = body.get(b"piece length", 0)
    pieces = body.get(b"pieces", b"")
    if not isinstance(piece_length, int) or not 0 < piece_length <= MAX_METADATA:
        raise FQDBError("Invalid or unsupported piece length")
    piece_count = (size + piece_length - 1) // piece_length
    if not isinstance(pieces, bytes) or len(pieces) != piece_count * 20:
        raise FQDBError("Invalid daily piece-hash coverage")
    fs = info.files()
    if (
        info_hash(info) != entry.info_hash
        or fs.num_files() != 1
        or name != Path(entry.file).name
        or fs.file_path(0) != name
        or fs.file_size(0) != size
    ):
        raise FQDBError("Torrent metadata does not match trusted daily manifest")
    if fs.file_flags(0) & lt.file_storage.flag_symlink:
        raise FQDBError("Symlinks in torrents are forbidden")
    return info


def validate_daily(payload, entry, root):
    return validate_info(inspect(payload, root), entry, root)


def metadata_torrent(info, trackers=()):
    # The native writer retains the original encoded info dictionary.
    params = lt.add_torrent_params()
    params.ti = info
    ordered = sorted(trackers, key=lambda tracker: tracker["tier"])
    params.trackers = [tracker["url"] for tracker in ordered]
    params.tracker_tiers = [tracker["tier"] for tracker in ordered]
    return lt.bencode(lt.write_torrent_file(params))


def make_magnet(entry, trackers=()):
    params = lt.add_torrent_params()
    params.info_hashes = lt.info_hash_t(lt.sha1_hash(bytes.fromhex(entry.info_hash)))
    params.name = Path(entry.file).name
    params.trackers = list(dict.fromkeys(trackers))
    return lt.make_magnet_uri(params)


def torrent_cache_path(root, entry):
    """Return this current-format content identity's immutable metadata file."""
    from ..publish.paths import immutable_metadata

    return immutable_metadata(root, entry)


_TOPIC = re.compile(r"xt(?:\.[0-9]*)?", flags=re.IGNORECASE | re.ASCII)
_V1_TOPIC = re.compile(r"urn:btih:([0-9a-f]{40}|[a-z2-7]{32})", flags=re.IGNORECASE | re.ASCII)


def parse_magnet(uri):
    """Pass magnet decoding to libtorrent after enforcing a single v1 identity."""
    if not isinstance(uri, str) or len(uri) > 16384:
        raise FQDBError("Invalid or oversized magnet link")
    try:
        value = urlparse(uri)
    except ValueError as exc:
        raise FQDBError("Invalid magnet link") from exc
    if value.scheme != "magnet" or value.netloc or value.path or value.fragment:
        raise FQDBError("Invalid magnet link")
    topics = []
    options = []
    for field in value.query.split("&"):
        name, _, payload = field.partition("=")
        if not _TOPIC.fullmatch(unquote_plus(name)):
            if field:
                options.append(field)
            continue
        topic = _V1_TOPIC.fullmatch(unquote_plus(payload))
        if not topic:
            raise FQDBError("Only valid v1 btih magnet hashes are supported")
        topics.append(topic[1])
    if not topics:
        raise FQDBError("Magnet requires one unambiguous v1 info-hash")
    # Some supported libtorrent versions accept conflicting xt fields by
    # retaining the last one. Give the parser one topic at a time.
    normalized = "&".join([f"magnet:?xt=urn:btih:{topics[0]}", *options])
    try:
        params = lt.parse_magnet_uri(normalized)
        identity = params.info_hashes
        if not identity.has_v1() or identity.has_v2():
            raise FQDBError("Invalid magnet hash")
        for topic in topics[1:]:
            other = lt.parse_magnet_uri(f"magnet:?xt=urn:btih:{topic}").info_hashes
            if other.v1 != identity.v1:
                raise FQDBError("Magnet requires one unambiguous v1 info-hash")
    except RuntimeError as exc:
        raise FQDBError("Invalid magnet link") from exc
    return params


def magnet_hash(uri):
    return str(parse_magnet(uri).info_hashes.v1)


def verify_pieces(info, path):
    """Check local bytes against the original torrent using libtorrent's disk checker."""
    path = Path(path)
    if info.num_files() != 1 or path.stat().st_size != info.total_size():
        return False

    # A short-lived offline check leaves active peers and their resume state alone.
    categories = lt.alert.category_t
    session = lt.session(
        {
            "listen_interfaces": "",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_tcp": False,
            "enable_outgoing_utp": False,
            "alert_mask": int(
                categories.error_notification
                | categories.status_notification
                | categories.storage_notification
            ),
        }
    )
    flags = lt.torrent_flags
    params = lt.add_torrent_params()
    params.ti = info
    params.save_path = str(path.parent.resolve())
    params.renamed_files = {0: path.name}
    params.flags &= ~(flags.auto_managed | flags.paused | flags.seed_mode)
    params.flags |= flags.upload_mode | flags.disable_pex | flags.override_trackers | flags.override_web_seeds
    session.add_torrent(params)
    # With no resume data, the native session checks the original piece hashes.
    while True:
        session.wait_for_alert(500)
        for alert in session.pop_alerts():
            if isinstance(alert, lt.torrent_checked_alert):
                return alert.handle.status().is_seeding
            if isinstance(alert, (lt.file_error_alert, lt.torrent_error_alert)):
                raise FQDBError(alert.message())
