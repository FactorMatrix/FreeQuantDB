"""Exercise default activation through independent CLI and Python processes."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

from filelock import FileLock, Timeout
import httpx
import libtorrent as lt
import pytest

from fqdb.bt.torrents import torrent_cache_path
from fqdb.publish.catalog import export_public_catalog
from fqdb.service.client import Client
from fqdb.storage.metadata import Store
from gateway_helpers import loopback_config
from magnet_helpers import mirror
from test_contracts import DAYS, setup_store
from test_gateway_autoseed import download_magnet
from test_gateway_autosync import share_ready, subscription
from test_gateway_lifecycle import shutdown, stopped, wait_for


INSPECT = """
import json
import fqdb
from fqdb.service.client import Client

def inspect():
    client = Client()
    endpoint = client.endpoint()
    status = fqdb.provider.status()
    return {
        "data_dir": str(client.config.root),
        "pid": endpoint["pid"] if endpoint else None,
        "provider": status["provider"],
        "limit": status["settings"]["upload_limit"],
        "priority": status["priority"],
    }
"""


def activation_env(root=None):
    env = os.environ.copy()
    env.pop("FQDB_AUTOSTART", None)
    if root is not None:
        env["FQDB_DATA_DIR"] = str(root)
    return env


def python_command(script="print(json.dumps(inspect()))", *args):
    return [sys.executable, "-c", f"{INSPECT}\n{script}", *map(str, args)]


def cli_command(*args):
    return [sys.executable, "-m", "fqdb", *map(str, args)]


def invoke(command, *, root=None, cwd=None, check=True):
    result = subprocess.run(
        command, env=activation_env(root), cwd=cwd, capture_output=True, text=True, timeout=60
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def run_cli(*args, **options):
    result = invoke(cli_command(*args), **options)
    return json.loads(result.stdout)


def inspect_import(**options):
    return json.loads(invoke(python_command(), **options).stdout)


def stop_work(*clients):
    for client in clients:
        if client.endpoint() is not None:
            shutdown(client)


def concurrent_imports(cwd):
    commands = [python_command() for _ in range(4)] + [cli_command("datasets")]
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        imports = [pool.submit(invoke, command, cwd=cwd) for command in commands]
        results = [json.loads(call.result().stdout) for call in imports]
    assert results[-1]
    return results[:-1]


def test_cli_saves_root_and_concurrent_python_imports_share_the_detached_worker(empty_data_root):
    selected = empty_data_root / "SYNTHETIC-selected"
    publisher, _, _ = setup_store(selected)
    client = Client(selected)
    wrong_cwd = empty_data_root / "SYNTHETIC-unrelated-cwd"
    fallback = empty_data_root / "SYNTHETIC-home" / ".fqdb"
    console = Path(sysconfig.get_path("scripts")) / ("fqdb.exe" if os.name == "nt" else "fqdb")
    wrong_cwd.mkdir()
    try:
        assert invoke([str(console), "--help"]).stdout
        assert run_cli("service", "stop", root=selected)["status"] == "stopped"
        assert client.endpoint() is None

        assert json.loads(invoke([str(console), "datasets", "--data-dir", str(selected)]).stdout)
        assert not fallback.exists()
        endpoint = wait_for(client.endpoint)
        for status in concurrent_imports(wrong_cwd):
            assert status["data_dir"] == str(selected) and status["pid"] == endpoint["pid"]
            assert status["limit"] == status["priority"]["global_upload_limit"] == 500_000
        assert run_cli("service", "status")["url"] == endpoint["url"]

        seeded = publisher.entries()[0].info_hash
        wait_for(lambda: share_ready(client, seeded))
        with httpx.Client(trust_env=False, timeout=3) as http:
            assert http.get(endpoint["url"] + "/").status_code in (403, 404, 503)

        assert run_cli("service", "stop")["status"] in ("stopping", "stopped")
        wait_for(lambda: stopped(client))
        assert run_cli("service", "status")["status"] == "stopped"
        assert invoke(cli_command("gateway", "--help")).stdout
        assert client.endpoint() is None
        restarted = concurrent_imports(wrong_cwd)
        assert {status["pid"] for status in restarted} == {client.endpoint()["pid"]}
        assert client.endpoint()["pid"] != endpoint["pid"]
        wait_for(lambda: share_ready(client, seeded))
    finally:
        stop_work(client)


def test_initial_import_can_switch_to_new_root_while_explicit_root_remains_independent(empty_data_root):
    implicit = empty_data_root / "SYNTHETIC-home" / ".fqdb"
    selected = empty_data_root / "SYNTHETIC-selected"
    explicit = empty_data_root / "SYNTHETIC-explicit-env"
    for path in (implicit, selected, explicit):
        loopback_config(path)
    setup_store(selected)
    old, new, other = map(Client, (implicit, selected, explicit))
    untouched = implicit / "data" / "SYNTHETIC-retained.txt"
    untouched.write_text("retained", encoding="utf8")
    code = """
import sys
import importlib
initial = inspect()
importlib.reload(fqdb)
assert inspect()["pid"] == initial["pid"]
selection = fqdb.init(sys.argv[1])
print(json.dumps((initial, inspect(), selection.get("previous_backgrounds", []))))
"""
    try:
        before, after, outgoing = json.loads(invoke(python_command(code, selected)).stdout)
        assert before["data_dir"] == str(implicit) and before["pid"]
        assert after["data_dir"] == str(selected) and after["pid"] and after["pid"] != before["pid"]
        assert outgoing == [{"data_dir": str(implicit), "status": "stopped"}]
        wait_for(lambda: stopped(old))
        assert untouched.read_text(encoding="utf8") == "retained"
        assert inspect_import()["pid"] == after["pid"]

        setup_store(explicit)
        before, after_override, outgoing = json.loads(
            invoke(python_command(code, selected), root=explicit).stdout
        )
        assert before["data_dir"] == str(explicit) and before["pid"]
        assert after_override["data_dir"] == str(selected) and after_override["pid"] == after["pid"]
        assert outgoing == [{"data_dir": str(explicit), "status": "kept", "reason": "explicit_root"}]
        endpoint = wait_for(other.endpoint)
        assert endpoint["pid"] == before["pid"]
        wait_for(lambda: share_ready(other, Store(explicit).entries()[0].info_hash))
        assert inspect_import()["data_dir"] == str(selected)
    finally:
        stop_work(old, new, other)


def test_import_keeps_custom_limit_priority_and_saved_sharing_pause(empty_data_root):
    root = empty_data_root / "SYNTHETIC-provider"
    publisher, _, _ = setup_store(root)
    client = Client(root)
    try:
        client.call("source", {"source": str(mirror(root))})
        client.call("subscribe", {})
        initial = inspect_import(root=root)
        assert initial["provider"] and initial["limit"] == 500_000
        wait_for(lambda: subscription(client)["body"].get("last_check"))
        code = """
assert fqdb.provider.start(upload_limit=319_000)["provider"]
fqdb.provider.set_priority("127.0.0.2")
print(json.dumps(inspect()))
"""
        configured = json.loads(invoke(python_command(code), root=root).stdout)
        assert configured["pid"] == initial["pid"]
        assert configured["limit"] == configured["priority"]["global_upload_limit"] == 319_000
        assert [rule["ip"] for rule in configured["priority"]["rules"]] == ["127.0.0.2"]

        assert not run_cli("provider", "stop", root=root)["provider"]
        paused = inspect_import(root=root)
        assert paused["pid"] == initial["pid"] and paused["provider"] is False
        run_cli("service", "stop", root=root)
        wait_for(lambda: stopped(client))
        restarted = inspect_import(root=root)
        assert restarted["pid"] != initial["pid"] and restarted["provider"] is False
        assert restarted["limit"] == 319_000
        assert [rule["ip"] for rule in restarted["priority"]["rules"]] == ["127.0.0.2"]
        assert not client.config.read()["auto_seed"]
        assert publisher.entries()
    finally:
        stop_work(client)


def test_import_alone_runs_scheduled_subscriptions_and_seeds_original_magnet_after_script_exit(
    empty_data_root,
):
    publisher, schema, _ = setup_store(empty_data_root / "SYNTHETIC-publisher")
    origin = Client(publisher.root)
    receiver = Client(loopback_config(empty_data_root / "SYNTHETIC-receiver").root)
    excluded = publisher.entries(start_date=DAYS[0], end_date=DAYS[0])[0]
    requested = publisher.entries(start_date=DAYS[1], end_date=DAYS[1])[0]
    catalog = empty_data_root / "SYNTHETIC-catalog"
    export_public_catalog(publisher, catalog, period=excluded.period)
    try:
        receiver.call("source", {"source": str(catalog)})
        seed = origin.call("provider_start")
        receiver.call("peers_add", {"ip": "127.0.0.1", "port": seed["listen_port"]})
        receiver.call(
            "subscribe",
            {
                "dataset": schema.id,
                "frequency": "1d",
                "download": True,
                "interval": 10,
                "start_date": DAYS[1],
                "end_date": DAYS[1],
            },
        )
        assert receiver.endpoint() is None
        status = inspect_import(root=receiver.config.root)
        first = wait_for(lambda: subscription(receiver)["body"].get("last_check"))
        assert not receiver.get("/api/status")["transfers"]

        export_public_catalog(publisher, catalog)
        wait_for(lambda: share_ready(receiver, requested.info_hash), timeout=30)
        assert receiver.endpoint()["pid"] == status["pid"]
        wait_for(lambda: subscription(receiver)["body"]["last_check"] > first)
        installed = Store(receiver.config.root)
        tasks = installed.tasks()
        assert [task["id"] for task in tasks] == [requested.info_hash]
        assert tasks[0]["body"]["entry"]["magnet"] == requested.magnet
        cached = lt.torrent_info(torrent_cache_path(receiver.config.root, requested).read_bytes())
        published = lt.torrent_info(torrent_cache_path(publisher.root, requested).read_bytes())
        assert str(cached.info_hashes().v1) == requested.info_hash
        assert bytes(cached.info_section()) == bytes(published.info_section())

        shutdown(origin)
        filename = Path(requested.file).name
        assert (
            download_magnet(
                empty_data_root / "SYNTHETIC-third-peer",
                requested.magnet,
                receiver.get("/api/status")["listen_port"],
                filename,
            )
            == (publisher.root / requested.file).read_bytes()
        )
        checked = subscription(receiver)["body"]["last_check"]
        wait_for(lambda: subscription(receiver)["body"]["last_check"] > checked, timeout=25)
        assert not subscription(receiver)["body"]["last_result"]["task_ids"]
        assert [(task["id"], task["attempts"]) for task in installed.tasks()] == [
            (requested.info_hash, tasks[0]["attempts"])
        ]
    finally:
        stop_work(receiver, origin)


def test_saved_unavailable_root_cannot_be_recreated_or_redirected(empty_data_root):
    root = empty_data_root / "SYNTHETIC-mounted-data"
    loopback_config(root)
    run_cli("service", "stop", "-d", root)
    original = root / "data" / "SYNTHETIC-keep.txt"
    original.write_text("retained", encoding="utf8")
    offline = root.with_name("SYNTHETIC-offline-device")
    root.rename(offline)

    for command in (python_command(), cli_command("datasets")):
        result = invoke(command, check=False)
        assert result.returncode != 0 and str(root) in result.stderr
        assert not root.exists()

    root.mkdir()
    for command in (python_command(), cli_command("datasets")):
        result = invoke(command, check=False)
        assert result.returncode != 0 and str(root) in result.stderr
        assert not tuple(root.iterdir())

    assert not (empty_data_root / "SYNTHETIC-home" / ".fqdb").exists()
    assert (offline / "data" / original.name).read_text(encoding="utf8") == "retained"


@pytest.mark.skipif(os.name == "nt", reason="Simulates POSIX mount loss by renaming open storage")
def test_active_worker_stops_when_selected_storage_is_replaced(empty_data_root):
    root = empty_data_root / "SYNTHETIC-mounted-data"
    publisher, _, _ = setup_store(root)
    client = Client(root)
    offline = root.with_name("SYNTHETIC-offline-device")
    first = publisher.entries()[0]
    original = (root / first.file).read_bytes()

    def owner_released():
        try:
            with FileLock(offline / ".state/state/service.lock", timeout=0):
                return True
        except Timeout:
            return False

    try:
        assert inspect_import(root=root)["pid"]
        wait_for(lambda: share_ready(client, first.info_hash))
        root.rename(offline)
        root.mkdir()
        wait_for(owner_released)
        assert not tuple(root.iterdir())
        failed = invoke(cli_command("service", "status", "-d", root), check=False)
        assert failed.returncode != 0 and str(root) in failed.stderr
        assert not tuple(root.iterdir())
        assert (offline / first.file).read_bytes() == original
    finally:
        if offline.exists():
            if root.exists():
                root.rename(root.with_name("SYNTHETIC-underlying-directory"))
            offline.rename(root)
        stop_work(client)
