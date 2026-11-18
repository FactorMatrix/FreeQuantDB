"""Explicit BT endpoints, configured separately from trusted HTTP catalogs."""

from fqdb import api


def add(ip, port, *, enabled=True, note=""):
    return api.client().call("peers_add", {"ip": ip, "port": int(port), "enabled": enabled, "note": note})


def list():
    return api.client().call("peers_list")


def remove(ip, port):
    return api.client().call("peers_remove", {"ip": ip, "port": int(port)})


def enable(ip, port, enabled=True):
    return api.client().call("peers_enable", {"ip": ip, "port": int(port), "enabled": enabled})
