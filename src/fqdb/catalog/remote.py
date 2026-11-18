"""Conditional reads for a configured HTTP(S) or filesystem catalog mirror."""

from base64 import b64decode, b64encode
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlparse, urlunparse

import httpx

from ..util import FQDBError, atomic_json, safe_path

MAX_CATALOG = 32 * 1024 * 1024


def source_root(source):
    source = str(source)
    value = urlparse(source)
    if value.scheme in ("http", "https"):
        if value.hostname in ("github.com", "www.github.com"):
            raise FQDBError("Use a GitHub Raw catalog URL, not a repository/blob HTML page")
        if not value.hostname or value.username is not None:
            raise FQDBError("Catalog mirror requires a host and cannot embed credentials")
        path = value.path.rstrip("/").removesuffix("/index.json")
        return urlunparse(value._replace(path=path, query="", fragment="")).rstrip("/")
    if value.scheme and not (len(value.scheme) == 1 and source[1:2] == ":"):
        raise FQDBError("Catalog sources must be local paths or HTTP(S) URLs")
    path = Path(source).expanduser().resolve()
    return str(path.parent if path.name == "index.json" else path)


def _cached(path):
    if path is None:
        return {}, None
    try:
        if path.stat().st_size > 2 * MAX_CATALOG:
            return {}, None
        entry = json.loads(path.read_bytes())
        payload = b64decode(entry["payload"], validate=True)
        if len(payload) > MAX_CATALOG:
            return {}, None
        return {
            key: entry[key] for key in ("etag", "last-modified") if isinstance(entry.get(key), str)
        }, payload
    except (OSError, ValueError, TypeError, KeyError):
        return {}, None


def _remote(root, relative, cache_dir, client):
    if any(char in relative for char in ("%", "?", "#")):
        raise FQDBError("URL escapes, queries and fragments are forbidden in catalog object paths")
    url = urljoin(root + "/", relative)
    cache = Path(cache_dir) / hashlib.sha256(url.encode()).hexdigest() if cache_dir is not None else None
    saved, cached_payload = _cached(cache)
    headers = {}
    if "etag" in saved:
        headers["If-None-Match"] = saved["etag"]
    elif "last-modified" in saved:
        headers["If-Modified-Since"] = saved["last-modified"]
    stream = httpx.stream if client is None else client.stream
    with stream("GET", url, timeout=20, follow_redirects=False, headers=headers) as response:
        if response.status_code == 304 and cached_payload is not None:
            return cached_payload
        response.raise_for_status()
        data = bytearray()
        for block in response.iter_bytes():
            data.extend(block)
            if len(data) > MAX_CATALOG:
                raise FQDBError("Catalog object exceeds size limit")
        if cache is not None and any(key in response.headers for key in ("etag", "last-modified")):
            validators = {
                key: response.headers[key] for key in ("etag", "last-modified") if key in response.headers
            }
            atomic_json(cache, validators | {"payload": b64encode(data).decode()})
        return bytes(data)


def read_source(source, relative, *, cache_dir=None, client=None):
    path = PurePosixPath(relative)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != relative
        or ".." in path.parts
        or "\\" in relative
        or ":" in relative
    ):
        raise FQDBError(f"Unsafe relative path: {relative}")
    root = source_root(source)
    if urlparse(root).scheme in ("http", "https"):
        return _remote(root, relative, cache_dir, client)
    target = safe_path(Path(root), relative)
    with target.open("rb") as stream:
        payload = stream.read(MAX_CATALOG + 1)
    if len(payload) > MAX_CATALOG:
        raise FQDBError("Catalog object exceeds size limit")
    return payload
