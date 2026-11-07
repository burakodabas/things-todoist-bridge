"""Configuration storage (JSON file in Application Support)."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict

DEFAULT_DIR = Path.home() / "Library" / "Application Support" / "ThingsTodoistBridge"

DIRECTIONS = ("both", "things_to_todoist", "todoist_to_things")
THINGS_SCOPES = ("project", "area", "tag")
TODOIST_SCOPES = ("project", "label")
ON_DELETE = ("unlink", "delete")
# Which tasks created in Todoist are brought into Things (edits to linked tasks always sync).
IMPORT_MODES = ("all", "assigned_to_me", "label", "none")
AREA_PROJECTS = ("sections", "flat")

DEFAULT_CONFIG: Dict[str, Any] = {
    "todoist_token": "",
    "things_auth_token": "",  # optional, enables clearing dates via the Things URL scheme
    "interval_seconds": 30,
    "paused": False,
    "launch_things": True,
    "port": 8765,
    "rules": [],
}

DEFAULT_RULE: Dict[str, Any] = {
    "id": "",
    "name": "",
    "enabled": True,
    # Things side: {"type": "project"|"area"|"tag", "id": "...", "name": "..."}
    "things": {"type": "project", "id": "", "name": ""},
    # Todoist side: {"type": "project"|"label", "id": "...", "name": "...",
    #                "create_project_id": "" (for label scope: where new tasks go)}
    "todoist": {"type": "project", "id": "", "name": "", "create_project_id": ""},
    "direction": "both",
    "sync_tags": True,
    "sync_when": True,
    "sync_deadline": True,
    "sync_notes": True,
    "on_delete": "unlink",
    "import_completed": False,
    # New Todoist tasks -> Things: all | assigned_to_me | label (has import_label) | none
    "import_mode": "all",
    "import_label": "",
    # Area rules: each Things project of the area becomes a Todoist section (or "flat": no sections)
    "area_projects": "sections",
    # Append Todoist comments to the Things notes (read-only block)
    "sync_comments": True,
    # Assign by tagging (or "@mentioning") a collaborator's name in Things
    "auto_assign": True,
    # In a shared Todoist project, tasks nobody else is assigned to are assigned to me
    "assign_me_by_default": True,
    # Extra Things tag <-> Todoist assignee pairs: [{"tag": "Mert", "uid": "123", "name": "Mert"}]
    "assignees": [],
}


def data_dir() -> Path:
    d = Path(os.environ.get("TTBRIDGE_HOME", DEFAULT_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalise_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_RULE))
    for key, value in (rule or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    if not out["id"]:
        out["id"] = uuid.uuid4().hex[:10]
    if out["direction"] not in DIRECTIONS:
        raise ValueError(f"invalid direction: {out['direction']}")
    if out["things"]["type"] not in THINGS_SCOPES:
        raise ValueError(f"invalid Things scope: {out['things']['type']}")
    if out["todoist"]["type"] not in TODOIST_SCOPES:
        raise ValueError(f"invalid Todoist scope: {out['todoist']['type']}")
    if out["on_delete"] not in ON_DELETE:
        raise ValueError(f"invalid on_delete: {out['on_delete']}")
    if out["import_mode"] not in IMPORT_MODES:
        raise ValueError(f"invalid import_mode: {out['import_mode']}")
    if out["import_mode"] == "label" and not str(out["import_label"]).strip():
        raise ValueError("import_mode 'label' needs import_label")
    if out["area_projects"] not in AREA_PROJECTS:
        raise ValueError(f"invalid area_projects: {out['area_projects']}")
    clean = []
    for a in out.get("assignees") or []:
        tag, uid = str(a.get("tag", "")).strip(), str(a.get("uid", "")).strip()
        if tag and uid:
            clean.append({"tag": tag, "uid": uid, "name": str(a.get("name") or "")})
    out["assignees"] = clean
    if not out["name"]:
        out["name"] = f"{out['things']['name']} ↔ {out['todoist']['name']}"
    return out


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "config.json")
        self._lock = threading.RLock()
        self.data: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                stored = json.loads(self.path.read_text("utf-8") or "{}")
                self.data.update(stored)
            self.data["rules"] = [normalise_rule(r) for r in self.data.get("rules", [])]
            env_token = os.environ.get("TODOIST_API_TOKEN")
            if env_token and not self.data.get("todoist_token"):
                self.data["todoist_token"] = env_token

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), "utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.path)

    def __getitem__(self, key):
        with self._lock:
            return self.data[key]

    def get(self, key, default=None):
        with self._lock:
            return self.data.get(key, default)

    def update(self, values: Dict[str, Any]) -> None:
        with self._lock:
            for key, value in values.items():
                if key == "rules":
                    value = [normalise_rule(r) for r in value]
                elif key not in DEFAULT_CONFIG:
                    continue
                self.data[key] = value
            self.save()

    def rules(self):
        with self._lock:
            return [dict(r) for r in self.data.get("rules", [])]

    def public(self) -> Dict[str, Any]:
        """Config safe to send to the browser (secrets masked)."""
        with self._lock:
            d = json.loads(json.dumps(self.data))
        for key in ("todoist_token", "things_auth_token"):
            d[key + "_set"] = bool(d.get(key))
            d[key] = ""
        return d
