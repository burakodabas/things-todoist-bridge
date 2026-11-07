"""Tiny local web UI + JSON API (stdlib only)."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Dict, Tuple
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import normalise_rule
from .service import BridgeService

log = logging.getLogger("ttbridge.web")

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _index_html() -> bytes:
    return resources.files("ttbridge").joinpath("static/index.html").read_bytes()


class Handler(BaseHTTPRequestHandler):
    service: BridgeService = None  # set by make_server
    server_version = "ttbridge/" + __version__

    def log_message(self, fmt, *args):  # keep the console quiet
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ---- helpers -----------------------------------------------------
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _send(self, code: int, body: Any, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ---- routing -----------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            code, body = self.route_get(url.path, q)
        except Exception as exc:
            log.exception("GET %s failed", url.path)
            code, body = 500, {"error": str(exc)}
        if isinstance(body, bytes):
            return self._send(code, body, "text/html")
        self._send(code, body)

    def do_POST(self):
        # The custom header can't be sent cross-origin without a CORS preflight,
        # which we never answer, so other websites can't drive this API.
        if not self._host_ok() or self.headers.get("X-TTBridge") != "1":
            return self._send(403, {"error": "forbidden"})
        url = urlparse(self.path)
        try:
            code, body = self.route_post(url.path, self._json_body())
        except ValueError as exc:
            code, body = 400, {"error": str(exc)}
        except Exception as exc:
            log.exception("POST %s failed", url.path)
            code, body = 500, {"error": str(exc)}
        self._send(code, body)

    def route_get(self, path: str, q: Dict[str, str]) -> Tuple[int, Any]:
        s = self.service
        if path in ("/", "/index.html"):
            return 200, _index_html()
        if path == "/api/status":
            return 200, {**s.snapshot_status(), "version": __version__}
        if path == "/api/config":
            return 200, s.config.public()
        if path == "/api/events":
            return 200, list(s.events)
        if path == "/api/logs":
            return 200, list(s.logs)[-200:]
        if path == "/api/links":
            return 200, s.links(q.get("rule") or None)
        if path == "/api/meta/things":
            return 200, s.things().meta()
        if path == "/api/meta/todoist":
            return 200, s.todoist().meta()
        return 404, {"error": "not found"}

    def route_post(self, path: str, body: Dict[str, Any]) -> Tuple[int, Any]:
        s = self.service
        if path == "/api/config":
            allowed = {"interval_seconds", "paused", "launch_things", "todoist_token", "things_auth_token"}
            values = {k: v for k, v in body.items() if k in allowed}
            for secret in ("todoist_token", "things_auth_token"):
                if secret in values and values[secret] in (None, ""):
                    values.pop(secret)  # empty field = keep the stored secret
                elif secret in values:
                    values[secret] = str(values[secret]).strip()
            if body.get("clear_things_auth_token"):
                values["things_auth_token"] = ""
            if "interval_seconds" in values:
                values["interval_seconds"] = max(10, int(values["interval_seconds"]))
            s.config.update(values)
            if "paused" in values and not values["paused"]:
                s.trigger()
            return 200, s.config.public()
        if path == "/api/rules/save":
            rule = normalise_rule(body.get("rule") or {})
            if not rule["things"].get("name") or not rule["todoist"].get("name"):
                raise ValueError("Pick both a Things source and a Todoist target")
            rules = [r for r in s.config.rules() if r["id"] != rule["id"]]
            existing = [r["id"] for r in s.config.rules()]
            if rule["id"] in existing:
                rules.insert(existing.index(rule["id"]), rule)
            else:
                rules.append(rule)
            s.config.update({"rules": rules})
            return 200, {"ok": True, "rule": rule}
        if path == "/api/rules/delete":
            rid = body.get("id")
            s.config.update({"rules": [r for r in s.config.rules() if r["id"] != rid]})
            s.remove_rule_links(rid)
            return 200, {"ok": True}
        if path == "/api/sync":
            result: Dict[str, Any] = {}
            t = threading.Thread(target=lambda: result.update(s.sync_now()), daemon=True)
            t.start()
            t.join(timeout=float(body.get("wait", 120)))
            return 200, result or {"ok": True, "pending": True}
        if path == "/api/links/unlink":
            s.unlink(body["things_id"])
            return 200, {"ok": True}
        if path == "/api/test":
            out: Dict[str, Any] = {}
            try:
                meta = s.things().meta()
                out["things"] = {"ok": True, "projects": len(meta["projects"]), "tags": len(meta["tags"])}
            except Exception as exc:
                out["things"] = {"ok": False, "error": str(exc)}
            try:
                client = s.todoist()
                client.refresh()
                out["todoist"] = {"ok": True, "projects": len(client.projects), "tasks": len(client.items)}
            except Exception as exc:
                out["todoist"] = {"ok": False, "error": str(exc)}
            return 200, out
        if path == "/api/todoist/reset-cache":
            s.todoist().reset_cache()
            return 200, {"ok": True}
        return 404, {"error": "not found"}


def make_server(service: BridgeService, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
