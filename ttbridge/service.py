"""Background sync service: polls both apps and runs the engine on an interval."""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from .config import Config, data_dir
from .engine import SyncEngine
from .state import State
from .things import ThingsClient
from .todoist import TodoistClient

log = logging.getLogger("ttbridge.service")


class RingHandler(logging.Handler):
    def __init__(self, buffer: Deque[Dict[str, Any]]):
        super().__init__(logging.INFO)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        self.buffer.append({"ts": record.created, "level": record.levelname,
                            "name": record.name, "message": record.getMessage()})


class BridgeService:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.state = State(data_dir() / "state.db")
        self.logs: Deque[Dict[str, Any]] = collections.deque(maxlen=400)
        self.events: Deque[Dict[str, Any]] = collections.deque(
            self.state.get("recent_events", []), maxlen=300)
        logging.getLogger("ttbridge").addHandler(RingHandler(self.logs))
        self._lock = threading.Lock()  # one sync at a time
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status: Dict[str, Any] = {
            "running": False, "syncing": False, "last_sync": None, "last_duration": None,
            "last_error": None, "next_sync": None, "last_reports": [],
        }
        self._todoist: Optional[TodoistClient] = None
        self._todoist_token = None

    # ---- clients ---------------------------------------------------------
    def things(self) -> ThingsClient:
        return ThingsClient(self.config.get("things_auth_token", ""), self.config.get("launch_things", True))

    def todoist(self) -> TodoistClient:
        token = self.config.get("todoist_token", "")
        if self._todoist is None or token != self._todoist_token:
            self._todoist = TodoistClient(token, self.state)
            self._todoist_token = token
        return self._todoist

    def _record_event(self, ev: Dict[str, Any]) -> None:
        self.events.appendleft(ev)
        log.info("[%s] %s: %s %s", ev.get("rule"), ev.get("action"), ev.get("title"),
                 ("(" + ev["detail"] + ")") if ev.get("detail") else "")

    # ---- sync ------------------------------------------------------------
    def sync_now(self) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "A sync is already running"}
        self.status["syncing"] = True
        started = time.time()
        try:
            rules = [r for r in self.config.rules() if r.get("enabled", True)]
            if not self.config.get("todoist_token"):
                raise RuntimeError("Set your Todoist API token in Settings first")
            engine = SyncEngine(self.things(), self.todoist(), self.state, self._record_event)
            reports = engine.run(rules)
            self.status.update(last_sync=time.time(), last_error=None, last_reports=reports)
            errors = [e for r in reports for e in r["errors"]]
            if errors:
                self.status["last_error"] = errors[0]
            return {"ok": True, "reports": reports}
        except Exception as exc:
            log.error("Sync failed: %s", exc)
            self.status.update(last_sync=time.time(), last_error=str(exc))
            return {"ok": False, "error": str(exc)}
        finally:
            self.status["syncing"] = False
            self.status["last_duration"] = round(time.time() - started, 2)
            self.state.set("recent_events", list(self.events)[:100])
            self._lock.release()

    def trigger(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        log.info("Sync loop started")
        while not self._stop.is_set():
            interval = max(10, int(self.config.get("interval_seconds", 30)))
            if not self.config.get("paused") and self.config.rules():
                self.sync_now()
            self.status["next_sync"] = time.time() + interval
            self._wake.wait(interval)
            self._wake.clear()
        log.info("Sync loop stopped")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ttbridge-sync", daemon=True)
        self._thread.start()
        self.status["running"] = True

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self.status["running"] = False

    # ---- helpers for the UI ---------------------------------------------
    def snapshot_status(self) -> Dict[str, Any]:
        s = dict(self.status)
        s["paused"] = bool(self.config.get("paused"))
        s["link_counts"] = self.state.counts()
        s["token_set"] = bool(self.config.get("todoist_token"))
        return s

    def links(self, rule_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.state.links(rule_id)

    def unlink(self, things_id: str) -> None:
        self.state.remove_link(things_id)

    def remove_rule_links(self, rule_id: str) -> None:
        self.state.remove_rule(rule_id)
