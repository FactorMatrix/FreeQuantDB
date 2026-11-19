"""Renewable dashboard access, separate from the lifetime of the BT session."""

from __future__ import annotations

import secrets
import threading
import time

from ..util import FQDBError


class Lifetime:
    ttl = 30.0

    def __init__(self, owner):
        self.owner = owner
        self.sessions = {}
        self.lock = threading.RLock()

    def _expire(self):
        current = time.monotonic()
        self.sessions = {k: v for k, v in self.sessions.items() if v["deadline"] > current}
        enabled = bool(self.sessions)
        if self.owner.gateway_enabled != enabled:
            self.owner.set_gateway(enabled)
        return enabled

    def has_gateway(self):
        with self.lock:
            return self._expire()

    def open(self, *, persistent=False):
        if type(persistent) is not bool:
            raise FQDBError("Persistent Gateway activation must be boolean")
        with self.lock:
            self.owner.activate_gateway(persistent=persistent)
            session_id = secrets.token_urlsafe(24)
            self.sessions[session_id] = {
                "deadline": time.monotonic() + self.ttl,
                "browser_seen": False,
                "connections": set(),
            }
            self._expire()
            return {"session_id": session_id, "active": True, "browser_seen": False}

    def heartbeat(self, session_id, browser=False):
        with self.lock:
            self._expire()
            session = self.sessions.get(session_id)
            if session is None:
                return {"session_id": session_id, "active": False}
            # Once a page has opened, its heartbeat owns this lease. A terminal
            # still waiting for the page must not keep a closed tab alive.
            if browser or not session["browser_seen"]:
                session["deadline"] = time.monotonic() + self.ttl
            session["browser_seen"] |= browser
            return {"session_id": session_id, "active": True, "browser_seen": session["browser_seen"]}

    def attach(self, session_id):
        with self.lock:
            self._expire()
            if session_id not in self.sessions:
                return None
            connection_id = secrets.token_urlsafe(12)
            self.sessions[session_id]["connections"].add(connection_id)
            self.heartbeat(session_id, browser=True)
            return connection_id

    def detach(self, session_id, connection_id):
        with self.lock:
            session = self.sessions.get(session_id)
            if session:
                session["connections"].discard(connection_id)
                if not session["connections"]:
                    self.close(session_id, grace=3)

    def close(self, session_id, grace=0):
        if not isinstance(grace, (float, int)) or not 0 <= grace <= 3:
            raise FQDBError("Gateway close grace must be between 0 and 3 seconds")
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id]["deadline"] = time.monotonic() + grace
            self._expire()
            return {"status": "closed", "gateway": bool(self.sessions)}

    def close_all(self):
        with self.lock:
            self.sessions.clear()
            self._expire()
