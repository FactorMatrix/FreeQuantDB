"""Real loopback upload contention on synthetic bytes, with retained SSD evidence.

No public tracker, DHT, public peer, real quotation or network reconfiguration is
involved. The native session owns all byte counters and bandwidth limits.
"""

import json
import os
from pathlib import Path
import socket
import tempfile
import time

import libtorrent as lt
import pytest

from fqdb.bt.priority import PriorityManager

EVIDENCE_ROOT = (
    Path(
        os.environ.get(
            "FQDB_TEST_EVIDENCE_ROOT",
            "/home/quant/colorful-data/FreeQuantDB-test-artifacts/priority",
        )
    )
    .expanduser()
    .resolve()
)
LIMIT = 384 * 1024
TARGET = "127.0.0.2"
NORMAL = "127.0.0.3"


def wait_for(predicate, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("Native loopback transfer condition timed out")


def native_session(ip, upload_limit=0):
    return lt.session(
        {
            "listen_interfaces": f"[{ip}]:0" if ":" in ip else f"{ip}:0",
            "outgoing_interfaces": ip,
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_incoming_utp": False,
            "enable_outgoing_utp": False,
            "enable_incoming_tcp": True,
            "enable_outgoing_tcp": True,
            "allow_multiple_connections_per_ip": True,
            "ignore_limits_on_local_network": False,
            "upload_rate_limit": upload_limit,
            "unchoke_slots_limit": 8,
            # Explicit loopback reconnection is part of this experiment. The
            # native default min_reconnect_time=60 exceeds its 20s wait budget.
            "min_reconnect_time": 1,
            "peer_connect_timeout": 3,
            "alert_mask": int(lt.alert.category_t.error_notification),
        }
    )


def add(session, info, directory):
    directory.mkdir(exist_ok=True)
    params = lt.add_torrent_params()
    params.ti = info
    params.save_path = str(directory)
    params.flags &= ~lt.torrent_flags.auto_managed
    params.flags &= ~lt.torrent_flags.paused
    return session.add_torrent(params)


def test_priority_real_contention_reconnect_delete_restart_and_same_ip():
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=EVIDENCE_ROOT, prefix="contention-"))
    seed = root / "seed"
    seed.mkdir()
    payload = seed / "SYNTHETIC-NOT-MARKET-DATA.bin"
    with payload.open("wb") as output:
        for _ in range(32):
            output.write(os.urandom(1024 * 1024))
    storage = lt.file_storage()
    storage.add_file(payload.name, payload.stat().st_size)
    creator = lt.create_torrent(storage, 256 * 1024, flags=lt.create_torrent.v1_only)
    creator.set_priv(True)
    lt.set_piece_hashes(creator, str(seed))
    torrent = root / "SYNTHETIC.torrent"
    torrent.write_bytes(lt.bencode(creator.generate()))
    info = lt.torrent_info(str(torrent))
    evidence = {
        "synthetic": True,
        "description": "Real native TCP peers competing for one globally limited synthetic torrent",
        "libtorrent": lt.__version__,
        "info_hash": str(info.info_hash()),
        "payload_bytes": payload.stat().st_size,
        "global_upload_limit": LIMIT,
        "phases": [],
        "transitions": {},
    }
    sessions = []

    def retain():
        (root / "result.json").write_text(json.dumps(evidence, indent=2))

    def sample(manager, label, seconds):
        records = [manager.status()]
        started = time.monotonic()
        for _ in range(seconds):
            time.sleep(1)
            records.append(manager.status())
        elapsed = time.monotonic() - started
        byte_delta = {}
        for ip in (TARGET, NORMAL):
            before = sum(p["uploaded"] for p in records[0]["connections"] if p["ip"] == ip)
            after = sum(p["uploaded"] for p in records[-1]["connections"] if p["ip"] == ip)
            byte_delta[ip] = after - before
        evidence["phases"].append(
            {"label": label, "seconds": elapsed, "bytes": byte_delta, "samples": records}
        )
        retain()
        assert all(value > 0 for value in byte_delta.values()), "Both native peers must actually receive data"
        assert all(record["global_upload_limit"] == LIMIT for record in records)
        # Allow for the native bandwidth allocator's initial burst.
        assert sum(byte_delta.values()) <= LIMIT * (elapsed + 2)
        return byte_delta

    try:
        source = native_session("127.0.0.1", LIMIT)
        sessions.append(source)
        manager = PriorityManager(source)
        source_handle = add(source, info, seed)
        wait_for(lambda: source_handle.status().is_seeding)
        clients = [native_session(TARGET), native_session(NORMAL)]
        sessions.extend(clients)
        handles = [add(client, info, root / f"receiver-{index}") for index, client in enumerate(clients)]

        def connect():
            wait_for(lambda: all(not (h.flags() & lt.torrent_flags.paused) for h in handles))
            last_connect = 0

            def connected():
                nonlocal last_connect
                if time.monotonic() - last_connect >= 0.5:
                    for handle in handles:
                        handle.connect_peer(("127.0.0.1", source.listen_port()))
                    last_connect = time.monotonic()
                return len(manager.status()["connections"]) == len(handles)

            try:
                return wait_for(connected)
            finally:
                evidence.setdefault("connection_attempts", []).append(
                    {
                        "source_port": source.listen_port(),
                        "connections": manager.status()["connections"],
                        "receivers": [
                            {
                                "peers": h.status().num_peers,
                                "paused": bool(h.flags() & lt.torrent_flags.paused),
                            }
                            for h in handles
                        ],
                        "alerts": [alert.message() for session in sessions for alert in session.pop_alerts()][
                            -200:
                        ],
                        "test_min_reconnect_time": 1,
                    }
                )
                retain()

        def reconnect():
            for handle in handles:
                handle.pause()
            wait_for(
                lambda: (
                    not manager.status()["connections"] and all(h.status().num_peers == 0 for h in handles)
                )
            )
            for handle in handles:
                handle.resume()
            connect()

        connect()
        time.sleep(2)
        sample(manager, "normal_before_rule", 6)
        rules = [{"ip": TARGET, "level": "high", "enabled": True, "note": "synthetic contention test"}]
        applied = manager.apply(rules)
        evidence["transitions"]["existing_connection_rule_added"] = applied
        retain()
        assert applied["rules"][0]["state"] == "pending_reconnect"
        assert not applied["rules"][0]["effective"]
        reconnect()
        status = manager.status()
        evidence["transitions"]["after_explicit_reconnect"] = status
        assert status["rules"][0]["state"] == "active"
        assert status["rules"][0]["native_upload_priority"] == 8
        assert status["rules"][0]["class_mask"] == 1 | (1 << manager.class_id)
        time.sleep(1)
        high = sample(manager, "high_after_reconnect", 12)
        assert high[TARGET] > high[NORMAL], "Observe a native scheduling advantage under sustained contention"
        disabled = manager.apply([rules[0] | {"enabled": False}])
        evidence["transitions"]["rule_disabled_existing_connection"] = disabled
        assert disabled["rules"][0]["state"] == "pending_reconnect"
        manager.apply(rules)
        assert manager.status()["rules"][0]["state"] == "active"
        deleted = manager.apply([])
        evidence["transitions"]["rule_deleted_existing_connection"] = deleted
        retain()
        assert any(peer["ip"] == TARGET for peer in deleted["pending_reconnect"])
        reconnect()
        assert not manager.status()["pending_reconnect"]
        assert all(peer["assigned_level"] == "normal" for peer in manager.status()["connections"])
        sample(manager, "normal_after_delete_and_reconnect", 6)

        # Persist normalized rules, replace the native source session and reload
        # exactly that persisted policy. The torrent and its info-hash stay fixed.
        rule_file = root / "priority_rules.json"
        rule_file.write_text(json.dumps(rules))
        for handle in handles:
            handle.pause()
        wait_for(lambda: not manager.status()["connections"])
        source.pause()
        source = native_session("127.0.0.1", LIMIT)
        sessions.append(source)
        manager = PriorityManager(source, json.loads(rule_file.read_text()))
        source_handle = add(source, info, seed)
        wait_for(lambda: source_handle.status().is_seeding)
        for handle in handles:
            handle.resume()
        connect()
        status = manager.status()
        evidence["transitions"]["persisted_rule_new_native_session"] = status
        assert status["rules"][0]["effective"]
        assert str(source_handle.info_hashes().v1) == evidence["info_hash"]
        source.apply_settings({"upload_rate_limit": 0})
        unlimited = manager.status()
        evidence["transitions"]["live_peer_global_limit_disabled"] = unlimited
        assert unlimited["rules"][0]["state"] == "waiting_rate_limit"
        assert not unlimited["rules"][0]["effective"]
        source.apply_settings({"upload_rate_limit": LIMIT})
        sample(manager, "persisted_priority_after_session_restart", 6)

        # IP means network address, not user identity: two independently bound
        # clients sharing an IP are assigned the same rule and aggregated live.
        same_ip = native_session(TARGET)
        sessions.append(same_ip)
        handles.append(add(same_ip, info, root / "receiver-same-ip"))
        handles[-1].connect_peer(("127.0.0.1", source.listen_port()))
        wait_for(lambda: manager.status()["rules"][0]["connections"] == 2)
        same = manager.status()
        evidence["transitions"]["two_independent_peers_same_ip"] = same
        assert same["rules"][0]["connections"] == 2
        assert len({tuple(peer["endpoint"]) for peer in same["connections"] if peer["ip"] == TARGET}) == 2
        assert all(peer["assigned_level"] == "high" for peer in same["connections"] if peer["ip"] == TARGET)
        assert same["rules"][0]["lifetime_uploaded"] is None
        evidence["passed"] = True
    finally:
        retain()
        for session in sessions:
            session.pause()
    print(f"Native priority evidence: {root / 'result.json'}")


def test_priority_real_ipv6_connection():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError as exc:
        pytest.skip(f"Host IPv6 loopback is unavailable: {exc}")
    EVIDENCE_ROOT.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=EVIDENCE_ROOT, prefix="ipv6-"))
    seed = root / "seed"
    seed.mkdir()
    payload = seed / "SYNTHETIC-IPV6.bin"
    payload.write_bytes(os.urandom(2 * 1024 * 1024))
    storage = lt.file_storage()
    storage.add_file(payload.name, payload.stat().st_size)
    creator = lt.create_torrent(storage, 256 * 1024, flags=lt.create_torrent.v1_only)
    creator.set_priv(True)
    lt.set_piece_hashes(creator, str(seed))
    info = lt.torrent_info(creator.generate())
    source = native_session("::1", 64 * 1024)
    receiver = native_session("::1")
    try:
        manager = PriorityManager(source, [{"ip": "::1", "note": "synthetic IPv6 loopback"}])
        handle = add(source, info, seed)
        wait_for(lambda: handle.status().is_seeding)
        client = add(receiver, info, root / "receiver")
        client.connect_peer(("::1", source.listen_port()))
        wait_for(lambda: (manager.status()["rules"][0]["uploaded"] or 0) > 0)
        status = manager.status()
        assert status["rules"][0]["state"] == "active"
        assert status["rules"][0]["ip"] == "::1"
        assert status["rules"][0]["native_upload_priority"] == 8
        (root / "result.json").write_text(
            json.dumps({"synthetic": True, "passed": True, "status": status}, indent=2)
        )
        print(f"Native IPv6 evidence: {root / 'result.json'}")
    finally:
        source.pause()
        receiver.pause()
