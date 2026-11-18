"""Copy one explicitly selected legacy publication into a magnet catalog."""

from pathlib import Path

from ..catalog.magnet_manifest import json_object
from ..catalog.models import Entry
from ..schema.base import Dataset
from ..util import FQDBError, safe_path
from .publisher import import_native_publication


def migrate_package(store, source, *, dataset, frequency, period):
    source = Path(source).expanduser().resolve()
    if source == store.root or source in store.root.parents or store.root in source.parents:
        raise FQDBError("Use separate source and destination roots for migration")
    manifest = json_object((source / "torrents/index.json").read_bytes())
    if manifest.get("schema_version") != 3 or not isinstance(manifest.get("entries"), list):
        raise FQDBError("Migration requires an original version 3 publication index")
    matches = [
        record
        for record in manifest["entries"]
        if (record.get("dataset"), record.get("frequency"), record.get("period"))
        == (dataset, frequency, period)
    ]
    if not matches:
        raise FQDBError("No legacy publication matches the requested dataset, frequency and period")
    chosen = max(matches, key=lambda record: record["version"])
    entry = Entry(**chosen).validate(store.root)
    schemas = [Dataset(**record) for record in manifest["datasets"] if record.get("id") == dataset]
    if len(schemas) != 1 or frequency not in schemas[0].frequencies:
        raise FQDBError("Original dataset schema is unavailable")
    metadata_path = Path(entry.file).relative_to("data").with_suffix(".torrent")
    metadata = safe_path(source / "torrents", metadata_path.as_posix()).read_bytes()
    data = safe_path(source, entry.file)
    previous = store.dataset(dataset, required=False)
    if previous is None or not previous.units_confirmed:
        store.register(schemas[0])
    elif previous.to_dict() != schemas[0].to_dict():
        raise FQDBError("The target dataset already has different publication metadata")
    return import_native_publication(store, entry, data, metadata)
