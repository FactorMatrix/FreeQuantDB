"""Discover, synchronize and use local magnet catalog packages."""


def add(source):
    from ..api import add_source

    return add_source(source)


def remove(source):
    from ..api import client

    return client().call("source_remove", {"source": str(source)})


def list_sources():
    from ..config import Config

    return Config().read()["sources"]


def entries(dataset=None, frequency=None, start_date=None, end_date=None):
    from ..util import day

    first, last = day(start_date) if start_date else None, day(end_date) if end_date else None
    return [
        item
        for item in scan()["packages"]
        if (not dataset or item["dataset"] == dataset)
        and (not frequency or item["frequency"] == frequency)
        and (not first or (item["end_date"] and item["end_date"] >= first))
        and (not last or (item["start_date"] and item["start_date"] <= last))
    ]


def export(output_dir, *, dry_run=False, **scope):
    from ..api import client

    return client().call(
        "catalog_export",
        {"output_dir": str(output_dir), "dry_run": dry_run, **scope},
    )


def scan(*, deep=False):
    from ..api import client

    return client().call("catalog_scan", {"deep": deep})


def refresh(source=None):
    from ..api import client

    return client().call("catalog_refresh", {"source": str(source)} if source is not None else {})


def import_local(source, *, relative=None):
    from ..api import client

    return client().call("catalog_import", {"source": str(source), "relative": relative})


def check(package_path):
    from ..api import client

    return client().call("catalog_check", {"catalog_path": str(package_path)})


def download(package_path, *, background=False, timeout=300):
    from ..api import client

    connection = client()
    result = connection.call("download_package", {"catalog_path": str(package_path)})
    return result if background else connection.wait(result, timeout)
