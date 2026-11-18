from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

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
    # Preserve the original bencoded info section byte-for-byte and thus its hash.
    tiers = {}
    for tracker in trackers:
        tiers.setdefault(tracker["tier"], []).append(tracker["url"].encode("utf-8"))
    extra = b""
    if tiers:
        announce = [tiers[tier] for tier in sorted(tiers)]
        extra = lt.bencode({b"announce": announce[0][0], b"announce-list": announce})[1:-1]
    return b"d" + extra + b"4:info" + bytes(info.info_section()) + b"e"


def make_magnet(entry, trackers=()):
    parameters = [("xt", "urn:btih:" + entry.info_hash), ("dn", Path(entry.file).name)]
    parameters.extend(("tr", url) for url in dict.fromkeys(trackers))
    return "magnet:?" + urlencode(parameters)


def torrent_cache_path(root, entry):
    """Return this current-format content identity's immutable metadata file."""
    from ..publish.paths import immutable_metadata

    return immutable_metadata(root, entry)


def magnet_hash(uri):
    if not isinstance(uri, str) or len(uri) > 16384:
        raise FQDBError("Invalid or oversized magnet link")
    value = urlparse(uri)
    if value.scheme != "magnet" or value.netloc or value.path or value.fragment:
        raise FQDBError("Invalid magnet link")
    hashes = []
    for xt in parse_qs(value.query).get("xt", []):
        if not xt.lower().startswith("urn:btih:"):
            raise FQDBError("Only v1 btih magnets are supported")
        h = xt[9:]
        try:
            raw = bytes.fromhex(h) if len(h) == 40 else base64.b32decode(h.upper()) if len(h) == 32 else b""
        except (ValueError, TypeError) as exc:
            raise FQDBError("Invalid magnet hash") from exc
        if len(raw) != 20:
            raise FQDBError("Invalid magnet hash")
        hashes.append(raw.hex())
    if not hashes or len(set(hashes)) != 1:
        raise FQDBError("Magnet requires one unambiguous v1 info-hash")
    return hashes[0]


def verify_pieces(info, path):
    with open(path, "rb") as f:
        for i in range(info.num_pieces()):
            if hashlib.sha1(f.read(info.piece_size(i))).digest() != info.hash_for_piece(i):
                return False
    return True
