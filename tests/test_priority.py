"""Validation and real native peer-class configuration; no libtorrent mocks."""

import json

import libtorrent as lt
import pytest

from fqdb.bt.priority import MAX_RULES, PriorityManager, canonical_ip, normalize_rules
from fqdb.util import FQDBError


@pytest.fixture
def native_session():
    session = lt.session(
        {
            "listen_interfaces": "127.0.0.1:0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "ignore_limits_on_local_network": False,
        }
    )
    yield session
    session.pause()


@pytest.mark.parametrize("value", ["host.example", "127.0.0.1:42", "192.0.2.0/24", "fe80::1%eth0", "", None])
def test_priority_rejects_non_ip(value):
    with pytest.raises(FQDBError):
        canonical_ip(value)


def test_priority_rule_validation_and_ipv6():
    assert canonical_ip(" 2001:0db8::1 ") == "2001:db8::1"
    assert canonical_ip("::ffff:192.0.2.1") == "192.0.2.1"
    for rules in (
        [{"ip": "192.0.2.1", "level": "urgent"}],
        [{"ip": "192.0.2.1", "enabled": "false"}],
        [{"ip": "192.0.2.1", "note": "x" * 241}],
        [{"ip": "192.0.2.1", "unknown": True}],
        [{}],
        [{"ip": "192.0.2.1"}, {"ip": "::ffff:192.0.2.1"}],
        [{"ip": f"2001:db8::{i:x}"} for i in range(MAX_RULES + 1)],
    ):
        with pytest.raises(FQDBError):
            normalize_rules(rules)


def test_priority_native_class_reused_filter_ipv6_and_no_limit(native_session):
    manager = PriorityManager(native_session, [{"ip": "192.0.2.1"}, {"ip": "2001:db8::1"}])
    status = manager.status()
    class_id = manager.class_id
    assert class_id < 32
    assert status["peer_classes_used"] == 1
    assert status["global_upload_limit"] == 0
    assert all(rule["state"] == "waiting_rate_limit" for rule in status["rules"])
    assert all(not rule["effective"] for rule in status["rules"])
    assert all(rule["upload_rate"] is None and rule["uploaded"] is None for rule in status["rules"])
    assert all(rule["lifetime_uploaded"] is None for rule in status["rules"])
    assert all(rule["class_mask"] == 1 | (1 << class_id) for rule in status["rules"])
    assert native_session.get_peer_class(class_id)["upload_priority"] == 8
    assert native_session.get_peer_class(class_id)["upload_limit"] == 0
    native_session.apply_settings({"upload_rate_limit": 123456})
    for i in range(40):
        manager.apply([{"ip": f"2001:db8::{i + 1:x}", "note": "synthetic target"}])
        assert manager.class_id == class_id
    assert manager.status()["rules"][0]["state"] == "waiting_connection"
    assert native_session.get_settings()["upload_rate_limit"] == 123456
    # Both enabling and reverting a rule reuse the original one native class.
    status = manager.apply([{"ip": "192.0.2.1", "enabled": False}])
    assert status["rules"][0]["state"] == "disabled"
    assert status["rules"][0]["class_mask"] == 1
    status = manager.apply([{"ip": "192.0.2.1", "level": "normal"}])
    assert status["rules"][0]["state"] == "normal"
    assert status["rules"][0]["native_upload_priority"] == 1
    assert manager.apply([])["rules"] == []
    assert native_session.get_peer_class(class_id)["upload_priority"] == 8


def test_priority_persistence_round_trip_and_invalid_edit_is_atomic(native_session, data_root):
    rules = [{"ip": "2001:db8::2", "enabled": False, "note": "synthetic"}, {"ip": "192.0.2.3"}]
    manager = PriorityManager(native_session, rules)
    path = data_root / "synthetic-priority-rules.json"
    path.write_text(json.dumps(manager.rules))
    assert normalize_rules(json.loads(path.read_text())) == manager.rules
    before = list(manager.rules)
    with pytest.raises(FQDBError):
        manager.apply([], upload_limit=-1)
    assert manager.rules == before
    with pytest.raises(FQDBError):
        manager.apply([{"ip": "not an ip"}])
    assert manager.rules == before


def test_priority_updates_preserve_the_session_access_filter(native_session):
    access = lt.ip_filter()
    access.add_rule("192.0.2.10", "192.0.2.19", 1)
    native_session.set_ip_filter(access)
    manager = PriorityManager(native_session, [{"ip": "192.0.2.10"}, {"ip": "192.0.2.20"}])
    for rules in (manager.rules, []):
        manager.apply(rules)
        filter_after = native_session.get_ip_filter()
        assert filter_after.access("192.0.2.10") == 1
        assert filter_after.access("192.0.2.20") == 0


def test_priority_refuses_exhausted_native_class_bits(native_session):
    # The native uint32 IP filter cannot address a class ID >= 32. Exercise
    # the real resource boundary rather than creating one class per rule.
    allocated = [native_session.create_peer_class(f"synthetic-{index}") for index in range(29)]
    assert allocated[-1] == 31
    with pytest.raises(FQDBError, match="0–31"):
        PriorityManager(native_session)
    assert native_session.get_settings()["upload_rate_limit"] == 0


def test_priority_invalid_initial_rules_do_not_allocate_native_classes(native_session):
    for _ in range(40):
        with pytest.raises(FQDBError):
            PriorityManager(native_session, [{"ip": "not an IP"}])
    assert native_session.create_peer_class("synthetic-capacity-check") == 3
