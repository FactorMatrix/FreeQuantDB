"""User-controlled data provision and actual native upload scheduling rules."""

from .layout import runtime_path

from fqdb import api


def start(datasets=None, upload_limit=None, **scope):
    """Explicitly prepare/share local data until stop(); no data returns disabled.

    Omitted options retain configured scope/limits; a fresh root shares all
    eligible local packages. No gateway or prior CLI initialization is needed.
    """
    args = dict(scope)
    if datasets is not None:
        args["datasets"] = datasets
    if upload_limit is not None:
        args["upload_limit"] = upload_limit
    return api.client().call("provider_start", args)


def run(datasets=None, upload_limit=None, *, data_dir=None, **scope):
    """Run local sharing in the foreground; Ctrl+C or script exit stops it.

    No eligible local data returns promptly with a truthful preparation result.
    data_dir uses init() and saves that directory as the Python/CLI selection.
    """
    import os
    import time

    if data_dir is not None:
        api.init(data_dir)
    client = api.client()
    parent_pid = os.getpid()
    result = start(datasets, upload_limit, parent_pid=parent_pid, **scope)
    if not result["provider"]:
        return result | {"enabled": False}
    try:
        while client.endpoint() is not None:
            status = client.get("/api/status")
            if not status["provider"] or status.get("provider_parent_pid") != parent_pid:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        if client.endpoint() is not None:
            client.call("provider_stop", {"parent_pid": parent_pid})
    return result | {
        "provider": False,
        "enabled": False,
        "provision": result["provision"] | {"status": "stopped", "message": "本次前台做种已结束。"},
    }


def stop():
    """Pause sharing and keep it paused on the next Gateway start."""
    client = api.client()
    from filelock import FileLock, Timeout
    import time
    from fqdb.util import FQDBError

    deadline = time.monotonic() + 45
    while client.endpoint() is None:
        try:
            with FileLock(runtime_path(client.config.root, "state") / "service.lock", timeout=0):
                settings = client.config.update(provider=False, auto_seed=False)
                return {
                    "provider": False,
                    "enabled": False,
                    "service_running": False,
                    "transfers": [],
                    "settings": settings,
                }
        except Timeout:
            # A stopped HTTP server may still be releasing native BT resources.
            # Wait rather than letting Client.call create a replacement owner.
            if time.monotonic() >= deadline:
                raise FQDBError("Existing service is still starting or stopping; retry when it has settled")
            time.sleep(0.1)
    return client.call("provider_stop") | {"enabled": False, "service_running": True}


def status():
    client = api.client()
    if client.endpoint() is None:
        settings = client.config.read()
        return {
            "provider": False,
            "enabled": False,
            "service_running": False,
            "transfers": [],
            "settings": settings,
            "priority": {
                "rules": settings.get("priority_rules", []),
                "effective": False,
                "reason": "service_not_running",
            },
        }
    raw = client.get("/api/status")
    status = {k: raw[k] for k in ("provider", "transfers", "settings")}
    return status | {
        "provision": raw["provision"],
        "enabled": status["provider"],
        "service_running": True,
        "priority": client.call("priority_list"),
    }


def set_priority(ip, level="high", *, enabled=True, note="", reconnect=False):
    return api.client().call(
        "priority_set", {"ip": ip, "level": level, "enabled": enabled, "note": note, "reconnect": reconnect}
    )


def priorities():
    return status()["priority"]


def remove_priority(ip):
    return api.client().call("priority_remove", {"ip": ip})


def enable_priority(ip, enabled=True):
    return api.client().call("priority_enable", {"ip": ip, "enabled": enabled})
