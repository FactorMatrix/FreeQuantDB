"""Real CLI checks under isolated test homes; never uses production data."""

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

import pytest

from fqdb.config import Config
from fqdb.service.client import Client
from test_contracts import DAYS, setup_store


def isolated_environment(root):
    environment = os.environ.copy()
    for name in ("FQDB_DATA_DIR", "FQD_DATA_DIR", "XDG_CONFIG_HOME"):
        environment.pop(name, None)
    environment["HOME"] = str(root / "SYNTHETIC-home")
    environment["FQDB_USER_CONFIG"] = str(root / "SYNTHETIC-user.json")
    return environment


def run_cli(root, environment, *arguments):
    return subprocess.run(
        [sys.executable, "-m", "fqdb", *map(str, arguments)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )


def success(root, environment, *arguments):
    result = run_cli(root, environment, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def test_first_command_automatically_initializes_the_default_home(data_root):
    environment = isolated_environment(data_root)
    result = success(data_root, environment, "status")
    expected = Path(environment["HOME"]) / ".fqdb"
    assert result["data_dir"] == str(expected)
    assert result["service_running"] is False and result["provider"] is False
    assert (expected / ".state/config.json").is_file()
    assert not (expected / ".state/state/service.json").exists()
    assert not Path(environment["FQDB_USER_CONFIG"]).exists()


def test_every_command_help_exposes_directory_options_without_initializing(data_root):
    environment = isolated_environment(data_root)
    selection = Path(environment["FQDB_USER_CONFIG"])
    selection.write_text("{invalid SYNTHETIC user config")
    unused = data_root / "SYNTHETIC must not be created"
    commands = (
        "init",
        "gateway",
        "service",
        "status",
        "logs",
        "datasets",
        "register",
        "plan",
        "sync",
        "download",
        "check",
        "repair",
        "update",
        "settings",
        "publish",
        "provider",
        "peers",
        "catalog",
        "sources",
        "jobs",
        "subscribe",
        "task",
    )
    for command in (None, *commands):
        for flag, help_option in (("-d", "-h"), ("--data-dir", "--help")):
            arguments = [flag, unused, *([command] if command else []), help_option]
            result = run_cli(data_root, environment, *arguments)
            assert result.returncode == 0, (arguments, result.stdout, result.stderr)
            assert re.search(r"(?m)^[ \t]+-d(?=[ ,\t])", result.stdout)
            assert "--data-dir DIRECTORY" in result.stdout
            assert "init saves" in result.stdout
        if command is not None:
            result = run_cli(data_root, environment, command, "-d", unused, "-h")
            assert result.returncode == 0, result.stdout + result.stderr
            assert "--data-dir DIRECTORY" in result.stdout
    assert not unused.exists()
    assert not Path(environment["HOME"]).exists()
    assert selection.read_text() == "{invalid SYNTHETIC user config"


def test_directory_aliases_and_placements_are_temporary_and_accept_real_paths(data_root):
    environment = isolated_environment(data_root)
    saved = data_root / "SYNTHETIC saved data"
    success(data_root, environment, "init", "-d", saved)
    for flag in ("-d", "--data-dir"):
        for before in (True, False):
            for value in (
                "SYNTHETIC relative data",
                "~/SYNTHETIC personal data",
                str(data_root / "SYNTHETIC absolute data"),
            ):
                arguments = [flag, value, "status"] if before else ["status", flag, value]
                result = success(data_root, environment, *arguments)
                expanded = value.replace("~/", environment["HOME"] + "/", 1)
                expected = Path(expanded)
                if not expected.is_absolute():
                    expected = data_root / expected
                assert result["data_dir"] == str(expected.resolve())
                assert not (expected / ".fqdb").exists()
                assert result["service_running"] is False
                assert success(data_root, environment, "status")["data_dir"] == str(saved)
    assert json.loads(Path(environment["FQDB_USER_CONFIG"]).read_text())["data_dir"] == str(saved)


def test_init_saves_both_aliases_and_environment_keeps_priority(data_root):
    environment = isolated_environment(data_root)
    for flag in ("-d", "--data-dir"):
        for before in (True, False):
            saved = data_root / ("SYNTHETIC saved " + flag.removeprefix("-") + str(before))
            arguments = [flag, saved, "init"] if before else ["init", flag, saved]
            assert success(data_root, environment, *arguments)["data_dir"] == str(saved)
            assert success(data_root, environment, "status")["data_dir"] == str(saved)
    selected_by_environment = data_root / "SYNTHETIC environment data"
    environment["FQDB_DATA_DIR"] = str(selected_by_environment)
    assert success(data_root, environment, "status")["data_dir"] == str(selected_by_environment)
    temporary = data_root / "SYNTHETIC temporary data"
    assert success(data_root, environment, "status", "-d", temporary)["data_dir"] == str(temporary)
    assert success(data_root, environment, "status")["data_dir"] == str(selected_by_environment)
    saved_with_environment = data_root / "SYNTHETIC saved with environment"
    success(data_root, environment, "init", "--data-dir", saved_with_environment)
    assert success(data_root, environment, "status")["data_dir"] == str(selected_by_environment)
    environment.pop("FQDB_DATA_DIR")
    assert success(data_root, environment, "status")["data_dir"] == str(saved_with_environment)


def test_bad_directory_errors_are_explicit_and_do_not_fall_back(data_root):
    environment = isolated_environment(data_root)
    result = run_cli(data_root, environment, "status", "-d", "")
    assert result.returncode != 0 and "cannot be empty" in result.stderr
    assert "Traceback" not in result.stderr
    parent = data_root / "SYNTHETIC ordinary file"
    parent.write_text("SYNTHETIC file, not a directory")
    result = run_cli(data_root, environment, "--data-dir", parent / "child", "status")
    assert result.returncode != 0 and str(parent) in result.stderr
    assert "Traceback" not in result.stderr
    assert not (Path(environment["HOME"]) / ".fqdb").exists()


def test_cli_background_seeding_enforces_the_default_limit_with_ip_priority(data_root):
    environment = isolated_environment(data_root)
    root = data_root / "SYNTHETIC background seed"
    _, schema, _ = setup_store(root)
    client = Client(root)
    cli = ("-d", root, "provider")
    try:
        started = success(data_root, environment, *cli, "start", "--dataset", schema.id + "/1d")
        assert started["provider"] and started["provider_parent_pid"] is None
        status = success(data_root, environment, *cli, "status")
        assert status["service_running"] and status["provider"]
        assert status["settings"]["upload_limit"] == status["priority"]["global_upload_limit"] == 1_000_000

        target = "192.0.2.10"
        priority = success(data_root, environment, *cli, "priority", "set", target)
        assert priority["global_upload_limit"] == 1_000_000
        assert [(rule["ip"], rule["level"]) for rule in priority["rules"]] == [(target, "high")]
        assert success(data_root, environment, *cli, "priority", "list")["rules"][0]["ip"] == target
        assert not success(data_root, environment, *cli, "stop")["provider"]
    finally:
        if client.endpoint() is not None:
            client.call("shutdown")


@pytest.mark.skipif(os.name == "nt", reason="Foreground interruption is verified with POSIX SIGINT")
def test_provider_run_without_options_preserves_saved_advanced_settings(data_root):
    environment = isolated_environment(data_root)
    root = data_root / "SYNTHETIC configured data"
    _, schema, _ = setup_store(root)
    settings = {
        "provider_scopes": [schema.id + "/1d"],
        "provider_start": DAYS[0],
        "provider_end": DAYS[0],
        "upload_limit": 12345,
    }
    Config(root).update(**settings)
    environment["FQDB_DATA_DIR"] = str(root)
    client = Client(root)
    output = data_root / "SYNTHETIC-provider-run.log"
    process = None
    try:
        with output.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "fqdb", "provider", "run"],
                cwd=data_root,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert process.poll() is None, output.read_text()
            if client.endpoint() is not None and client.get("/api/status")["provider"]:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Foreground provision did not start: " + output.read_text())
        status = client.get("/api/status")
        assert {name: status["settings"][name] for name in settings} == settings
        assert status["provider_parent_pid"] == process.pid
        deadline = time.monotonic() + 5
        while "Ctrl+C" not in output.read_text() and time.monotonic() < deadline:
            time.sleep(0.05)
        first_line = output.read_text().splitlines()[0]
        assert "Ctrl+C 停止" in first_line
        assert "正在检查本地数据" in first_line or "可提供 1 个数据包，等待连接" in first_line
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            provision = client.get("/api/status")["provision"]
            if provision["status"] == "waiting_peers":
                break
            time.sleep(0.1)
        assert provision["status"] == "waiting_peers" and provision["eligible"] == 1
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=20) == 0, output.read_text()
        finished = json.loads(output.read_text().split("\n", 1)[1])
        assert finished["provider"] is False and finished["provision"]["status"] == "stopped"
        assert "datasets" not in finished and "settings" not in finished
        deadline = time.monotonic() + 15
        endpoint_file = root / ".state/state/service.json"
        while endpoint_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert client.endpoint() is None and not endpoint_file.exists()
        assert {name: Config(root).read()[name] for name in settings} == settings
        assert not Path(environment["FQDB_USER_CONFIG"]).exists()
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        if client.endpoint() is not None:
            client.call("shutdown")
