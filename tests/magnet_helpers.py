"""Export isolated synthetic publisher catalogs for consumer integration tests."""

from pathlib import Path

from fqdb.publish.catalog import export_public_catalog
from fqdb.storage.metadata import Store


def mirror(root):
    root = Path(root)
    output = root.with_name(root.name + "-synthetic-catalog")
    export_public_catalog(Store(root), output)
    return output
