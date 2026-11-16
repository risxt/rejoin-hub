"""Firebase Auth + Realtime Database over plain HTTPS (stdlib only).

The agent signs in with a refresh token (from the pairing code), keeps a fresh ID token, and talks to the Realtime
Database REST API. Commands arrive through the REST streaming API (Server-Sent Events).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger("rh.firebase")

SECURETOKEN_URL = "https://securetoken.googleapis.com/v1/token?key={key}"
SERVER_TIMESTAMP = {".sv": "timestamp"}


class FirebaseError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class Firebase:
    def __init__(self, api_key: str, db_url: str, refresh_token: str, timeout: float = 20.0):
        self.api_key = api_key
        self.db_url = db_url.rstrip("/")
        self._refresh_token = refresh_token
        self._id_token: str | None = None
        self._expires_at = 0.0
        self.uid: str | None = None
        self.timeout = timeout
        self._lock = threading.Lock()
        self.on_refresh_token: Callable[[str], None] | None = None  # persist rotated refresh tokens

    # ---- auth -------------------------------------------------------------------------------------------------
    def id_token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._id_token or time.time() > self._expires_at - 300:
                self._refresh()
            assert self._id_token
            return self._id_token

    def _refresh(self) -> None:
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self._refresh_token}).encode()
        req = urllib.request.Request(SECURETOKEN_URL.format(key=urllib.parse.quote(self.api_key)), data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = _send(req, self.timeout)
        self._id_token = data["id_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        self.uid = data.get("user_id")
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            if self.on_refresh_token:
                self.on_refresh_token(new_refresh)

    # ---- REST ---------------------------------------------------------------------------------------------------
    def _url(self, path: str, extra: dict[str, str] | None = None) -> str:
        q = {"auth": self.id_token()}
        if extra:
            q.update(extra)
        return f"{self.db_url}/{path.strip('/')}.json?{urllib.parse.urlencode(q)}"

    def request(self, method: str, path: str, value: Any = None, query: dict[str, str] | None = None) -> Any:
        for attempt in (1, 2):
            body = None if value is None and method in ("GET", "DELETE") else json.dumps(value, separators=(",", ":")).encode()
            req = urllib.request.Request(self._url(path, query), data=body, method=method,
                                         headers={"Content-Type": "application/json"})
            try:
                return _send(req, self.timeout)
            except FirebaseError as e:
                if e.status == 401 and attempt == 1:
                    self.id_token(force=True)
                    continue
                raise
        return None

    def get(self, path: str, **query: str) -> Any:
        return self.request("GET", path, query=query or None)

    def put(self, path: str, value: Any) -> Any:
        return self.request("PUT", path, value, {"print": "silent"})

    def patch(self, path: str, value: dict) -> Any:
        return self.request("PATCH", path, value, {"print": "silent"})

    def post(self, path: str, value: Any) -> str | None:
        res = self.request("POST", path, value)
        return res.get("name") if isinstance(res, dict) else None

    def delete(self, path: str) -> None:
        self.request("DELETE", path, query={"print": "silent"})

    # ---- streaming ----------------------------------------------------------------------------------------------
    def stream(self, path: str, on_event: Callable[[str, str, Any], None], stop: threading.Event) -> None:
        """Blocks until `stop` is set. Calls on_event(event, path, data) for put/patch events. Reconnects with backoff."""
        backoff = 2.0
        while not stop.is_set():
            try:
                req = urllib.request.Request(self._url(path), headers={"Accept": "text/event-stream"})
                # The server sends keep-alive every ~30 s, so a 90 s read timeout means the stream is dead.
                with urllib.request.urlopen(req, timeout=90) as resp:
                    backoff = 2.0
                    event = None
                    for raw in resp:
                        if stop.is_set():
                            return
                        line = raw.decode("utf-8", "replace").rstrip("\r\n")
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:") and event:
                            payload = line[5:].strip()
                            if event in ("put", "patch"):
                                msg = json.loads(payload)
                                on_event(event, msg.get("path", "/"), msg.get("data"))
                            elif event == "auth_revoked":
                                self.id_token(force=True)
                                break  # reconnect with the new token
                            elif event == "cancel":
                                raise FirebaseError(403, f"stream cancelled: {payload}")
                            event = None
            except FirebaseError as e:
                if e.status == 401:
                    self._safe_force_refresh()
                log.warning("stream %s: %s", path, e)
            except Exception as e:  # network drop, timeout, DNS
                log.warning("stream %s dropped: %s", path, e)
            if stop.wait(backoff):
                return
            backoff = min(backoff * 2, 60.0)

    def _safe_force_refresh(self) -> None:
        try:
            self.id_token(force=True)
        except Exception as e:
            log.warning("token refresh failed: %s", e)


def _send(req: urllib.request.Request, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        try:
            j = json.loads(detail)
            detail = j.get("error", {}).get("message") if isinstance(j.get("error"), dict) else j.get("error", detail)
        except ValueError:
            pass
        raise FirebaseError(e.code, str(detail)) from None
    if not raw:
        return None
    return json.loads(raw)
