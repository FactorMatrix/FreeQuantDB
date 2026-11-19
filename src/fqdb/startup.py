"""Activate the selected data root from Python and the command line."""

import logging
import os
import sys

from filelock import FileLock

from .config import DataRootUnavailable, resolve_root, saved_root, select_root, user_config_path
from .layout import runtime_path
from .service.client import Client

log = logging.getLogger(__name__)


def autostart_enabled():
    return os.getenv("FQDB_AUTOSTART") != "0"


def _module_entrypoint():
    if sys.argv and sys.argv[0] == "-m":
        original = sys.orig_argv
        return any(
            flag == "-m" and module in {"fqdb", "fqdb.__main__", "fqdb.service.daemon"}
            for flag, module in zip(original, original[1:])
        )
    return False


def _release(previous):
    try:
        if not (runtime_path(previous, "state") / "service.json").is_file():
            return None
        result = Client(previous).release_implicit()
    except DataRootUnavailable as exc:
        result = {"status": "unavailable", "reason": str(exc)}
    if result["status"] in {"kept", "draining", "unavailable"}:
        log.warning(
            "Previous FQDB background at %s: %s (%s)", previous, result["status"], result.get("reason")
        )
    return {"data_dir": str(previous), **result}


def select_client(root=None, *, persist=False, activate=False, opens_service=False):
    """Serialize root selection and background activation across entry points."""
    selection = user_config_path()
    selection.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(selection.with_name(selection.name + ".selection.lock"), timeout=90):
        try:
            previous = resolve_root()
        except DataRootUnavailable:
            if root is None:
                raise
            previous = None
        saved = saved_root()
        client = Client(root)
        config = client.config
        save_selection = persist or (activate and not saved and not os.getenv("FQDB_DATA_DIR"))
        if save_selection or activate or opens_service:
            # The child validates the recorded storage before it can open its
            # runtime paths, including if the mount disappears during launch.
            config.remember()
        outgoing = []
        old_roots = dict.fromkeys(
            candidate for candidate in (previous, saved) if candidate not in (None, config.root)
        )
        if persist and old_roots:
            # Start the new owner without authorizing transfers. A bind/startup
            # failure leaves the previous owner and saved selection available.
            if activate:
                client.ensure()
            for candidate in old_roots:
                if result := _release(candidate):
                    outgoing.append(result)
        if activate:
            client.activate(explicit=root is not None or bool(os.getenv("FQDB_DATA_DIR")))
        if save_selection:
            config.remember(select=True)
        select_root(config.root, persist=False)
        return client, outgoing


def start_on_import():
    if autostart_enabled() and not getattr(sys, "_fqdb_console_import", False) and not _module_entrypoint():
        select_client(activate=True)
