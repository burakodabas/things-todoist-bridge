"""Todoist adapter built on the Todoist API v1 Sync endpoint.

The Sync endpoint gives incremental updates through a ``sync_token``, so each
poll only transfers what changed since the last one. A local cache of items,
projects and labels is persisted in the state DB so restarts stay incremental.
Writes are queued as Sync commands and sent in one request.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .models import Item

log = logging.getLogger("ttbridge.todoist")

SYNC_URL = "https://api.todoist.com/api/v1/sync"
RESOURCES = ["items", "projects", "labels", "sections", "notes", "collaborators", "collaborator_states", "user"]
MAX_COMMANDS = 90  # Todoist allows 100 per request


class TodoistError(RuntimeError):
    pass


def _parse_ts(value: Optional[str]) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _date_part(obj: Optional[Dict[str, Any]]) -> Optional[str]:
    if not obj or not obj.get("date"):
        return None
    return obj["date"][:10]


def to_item(raw: Dict[str, Any]) -> Item:
    due = raw.get("due") or None
    return Item(
        id=str(raw["id"]),
        title=raw.get("content") or "",
        notes=raw.get("description") or "",
        when=_date_part(due),
        deadline=_date_part(raw.get("deadline")),
        tags=raw.get("labels") or [],
        completed=bool(raw.get("checked")),
        deleted=bool(raw.get("is_deleted")),
        modified=max(_parse_ts(raw.get("updated_at")), _parse_ts(raw.get("completed_at"))),
        recurring=bool(due and due.get("is_recurring")),
        project=str(raw.get("project_id")) if raw.get("project_id") else None,
        extra={"due": due,
               "section_id": str(raw["section_id"]) if raw.get("section_id") else None,
               "responsible_uid": str(raw["responsible_uid"]) if raw.get("responsible_uid") else None,
               "note_count": raw.get("note_count") or 0},
    )


class TodoistClient:
    def __init__(self, token: str, state=None, opener=None):
        self.token = token
        self.state = state
        self._opener = opener or urllib.request.urlopen
        self.sync_token = "*"
        self.items: Dict[str, Dict[str, Any]] = {}
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.labels: Dict[str, Dict[str, Any]] = {}
        self.sections: Dict[str, Dict[str, Any]] = {}
        self.notes: Dict[str, Dict[str, Any]] = {}  # task comments
        self.collaborators: Dict[str, Dict[str, Any]] = {}
        self.collaborator_states: Dict[str, Dict[str, Any]] = {}  # "project:user" -> state
        self.user: Dict[str, Any] = {}
        self._queue: List[Dict[str, Any]] = []
        if state is not None:
            cache = state.get("todoist_cache") or {}
            if cache.get("token_hint") == self._token_hint():
                self.sync_token = cache.get("sync_token", "*")
                self.items = cache.get("items", {})
                self.projects = cache.get("projects", {})
                self.labels = cache.get("labels", {})
                self.sections = cache.get("sections", {})
                self.notes = cache.get("notes", {})
                self.collaborators = cache.get("collaborators", {})
                self.collaborator_states = cache.get("collaborator_states", {})
                self.user = cache.get("user", {})
                if "sections" not in cache:  # cache from an older version: resync everything
                    self.sync_token = "*"

    def _token_hint(self) -> str:
        return (self.token or "")[-6:]

    # ---- transport -------------------------------------------------------
    def _post(self, form: Dict[str, Any]) -> Dict[str, Any]:
        if not self.token:
            raise TodoistError("Todoist API token is not set")
        body = urllib.parse.urlencode(
            {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in form.items()}
        ).encode()
        req = urllib.request.Request(
            SYNC_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "things-todoist-bridge",
            },
            method="POST",
        )
        for attempt in range(3):
            try:
                with self._opener(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:500]
                if exc.code == 429 or exc.code >= 500:
                    wait = int(exc.headers.get("Retry-After", 0) or 0) or 2 ** (attempt + 2)
                    log.warning("Todoist HTTP %s, retrying in %ss", exc.code, wait)
                    time.sleep(min(wait, 60))
                    continue
                if exc.code in (401, 403):
                    raise TodoistError("Todoist rejected the API token (HTTP %s)" % exc.code) from exc
                raise TodoistError(f"Todoist HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt == 2:
                    raise TodoistError(f"Cannot reach Todoist: {exc.reason}") from exc
                time.sleep(2 ** (attempt + 1))
        raise TodoistError("Todoist request failed after retries")

    # ---- reads -----------------------------------------------------------
    def refresh(self) -> Dict[str, int]:
        """Pull changes since the last sync token into the local cache."""
        data = self._post({"sync_token": self.sync_token, "resource_types": RESOURCES})
        changed = self._absorb(data)
        self._persist()
        return changed

    def _absorb(self, data: Dict[str, Any]) -> Dict[str, int]:
        if data.get("full_sync"):
            self.items, self.projects, self.labels, self.sections, self.notes = {}, {}, {}, {}, {}
            self.collaborators, self.collaborator_states = {}, {}
        for raw in data.get("items") or []:
            self.items[str(raw["id"])] = raw
        for store, key in ((self.projects, "projects"), (self.labels, "labels"), (self.sections, "sections"),
                           (self.notes, "notes"), (self.collaborators, "collaborators")):
            for raw in data.get(key) or []:
                if raw.get("is_deleted") or raw.get("is_archived"):
                    store.pop(str(raw["id"]), None)
                else:
                    store[str(raw["id"])] = raw
        for raw in data.get("collaborator_states") or []:
            k = f"{raw.get('project_id')}:{raw.get('user_id')}"
            if raw.get("is_deleted") or raw.get("state") == "deleted":
                self.collaborator_states.pop(k, None)
            else:
                self.collaborator_states[k] = raw
        if data.get("user"):
            self.user = data["user"]
        if data.get("sync_token"):
            self.sync_token = data["sync_token"]
        # Drop old completed/deleted items that nothing links to, to keep the cache small.
        if self.state is not None:
            linked = self.state.linked_todoist_ids()
            for id_ in [i for i, r in self.items.items()
                        if (r.get("checked") or r.get("is_deleted")) and i not in linked]:
                del self.items[id_]
            for id_ in [i for i, n in self.notes.items() if str(n.get("item_id")) not in self.items]:
                del self.notes[id_]
        return {"items": len(data.get("items") or []), "full_sync": int(bool(data.get("full_sync")))}

    def _persist(self) -> None:
        if self.state is None:
            return
        self.state.set("todoist_cache", {
            "token_hint": self._token_hint(),
            "sync_token": self.sync_token,
            "items": self.items,
            "projects": self.projects,
            "labels": self.labels,
            "sections": self.sections,
            "notes": self.notes,
            "collaborators": self.collaborators,
            "collaborator_states": self.collaborator_states,
            "user": self.user,
        })

    def reset_cache(self) -> None:
        self.sync_token = "*"
        self.items, self.projects, self.labels, self.sections, self.notes = {}, {}, {}, {}, {}
        self.collaborators, self.collaborator_states, self.user = {}, {}, {}
        self._persist()

    def meta(self) -> Dict[str, Any]:
        if not self.projects:
            self.refresh()
        projects = sorted(
            ({"id": p["id"], "name": p["name"], "inbox": bool(p.get("inbox_project"))}
             for p in self.projects.values() if not p.get("is_archived")),
            key=lambda p: (not p["inbox"], p.get("name", "").casefold()),
        )
        labels = sorted({l["name"] for l in self.labels.values()}, key=str.casefold)
        people = {}
        for st in self.collaborator_states.values():
            if st.get("state", "active") != "active":
                continue
            uid = str(st.get("user_id"))
            if uid == self.my_uid():
                continue
            c = self.collaborators.get(uid, {})
            people.setdefault(uid, {"id": uid, "name": c.get("full_name") or c.get("email") or uid,
                                    "email": c.get("email", ""), "projects": []})
            people[uid]["projects"].append(str(st.get("project_id")))
        return {"projects": projects, "labels": labels,
                "collaborators": sorted(people.values(), key=lambda p: p["name"].casefold()),
                "me": {"id": self.my_uid(), "name": self.user.get("full_name", "")}}

    def people_in(self, project_id: Optional[str]) -> List[Dict[str, str]]:
        """Active collaborators of a shared project (including me)."""
        out = []
        for st in self.collaborator_states.values():
            if str(st.get("project_id")) != str(project_id) or st.get("state", "active") != "active":
                continue
            uid = str(st.get("user_id"))
            c = self.collaborators.get(uid) or ({"full_name": self.user.get("full_name"), "email": self.user.get("email")}
                                                if uid == self.my_uid() else {})
            out.append({"id": uid, "name": c.get("full_name") or "", "email": c.get("email") or ""})
        return out

    def my_uid(self) -> str:
        return str(self.user.get("id") or "")

    def user_name(self, uid: Optional[str]) -> str:
        uid = str(uid or "")
        if uid and uid == self.my_uid():
            return self.user.get("full_name") or "me"
        c = self.collaborators.get(uid)
        return (c.get("full_name") or c.get("email")) if c else "someone"

    def sections_of(self, project_id: Optional[str]) -> Dict[str, Dict[str, Any]]:
        return {i: s for i, s in self.sections.items() if str(s.get("project_id")) == str(project_id)}

    def comments(self, item_id: str) -> List[Dict[str, Any]]:
        out = [n for n in self.notes.values() if str(n.get("item_id")) == str(item_id)]
        return sorted(out, key=lambda n: (n.get("posted_at") or "", str(n.get("id"))))

    def inbox_id(self) -> Optional[str]:
        for p in self.projects.values():
            if p.get("inbox_project"):
                return str(p["id"])
        return None

    def scope_items(self, scope: Dict[str, Any]) -> Dict[str, Item]:
        out = {}
        for raw in self.items.values():
            if raw.get("is_deleted") or raw.get("checked"):
                continue
            if scope["type"] == "project" and str(raw.get("project_id")) != str(scope["id"]):
                continue
            if scope["type"] == "label" and scope["name"].casefold() not in {
                    l.casefold() for l in raw.get("labels") or []}:
                continue
            it = to_item(raw)
            out[it.id] = it
        return out

    def get_item(self, id_: str) -> Optional[Item]:
        raw = self.items.get(str(id_))
        return to_item(raw) if raw else None

    # ---- writes ----------------------------------------------------------
    def _cmd(self, type_: str, args: Dict[str, Any], temp_id: Optional[str] = None) -> str:
        cmd = {"type": type_, "uuid": str(uuid.uuid4()), "args": args}
        if temp_id:
            cmd["temp_id"] = temp_id
        self._queue.append(cmd)
        return cmd["uuid"]

    @staticmethod
    def _date_args(fields: Dict[str, Any], current: Optional[Item]) -> Dict[str, Any]:
        args: Dict[str, Any] = {}
        if "when" in fields:
            if current is not None and current.recurring:
                pass  # never overwrite a recurring due date
            elif fields["when"]:
                args["due"] = {"date": fields["when"]}
            else:
                args["due"] = None
        if "deadline" in fields:
            args["deadline"] = {"date": fields["deadline"]} if fields["deadline"] else None
        return args

    def add(self, project_id: Optional[str], fields: Dict[str, Any]) -> str:
        """Queue a new task. Besides the common fields, ``_section`` (section id) and
        ``_responsible`` (user id) are understood."""
        temp_id = str(uuid.uuid4())
        args: Dict[str, Any] = {"content": fields.get("title", "")}
        if project_id:
            args["project_id"] = project_id
        if fields.get("_section"):
            args["section_id"] = fields["_section"]
        if fields.get("_responsible"):
            args["responsible_uid"] = fields["_responsible"]
        if fields.get("notes"):
            args["description"] = fields["notes"]
        if fields.get("tags"):
            args["labels"] = list(fields["tags"])
        args.update({k: v for k, v in self._date_args(fields, None).items() if v is not None})
        self._cmd("item_add", args, temp_id)
        if fields.get("completed"):
            self._cmd("item_close", {"id": temp_id})
        return temp_id

    def update(self, id_: str, fields: Dict[str, Any]) -> None:
        current = self.get_item(id_)
        args: Dict[str, Any] = {"id": id_}
        if "title" in fields:
            args["content"] = fields["title"]
        if "notes" in fields:
            args["description"] = fields["notes"]
        if "tags" in fields:
            args["labels"] = list(fields["tags"])
        args.update(self._date_args(fields, current))
        if "_responsible" in fields:
            args["responsible_uid"] = fields["_responsible"] or None
        if len(args) > 1:
            self._cmd("item_update", args)
        if "_section" in fields or "_project" in fields:
            # item_move takes exactly one destination: a section, or a project (= no section).
            if fields.get("_section"):
                self._cmd("item_move", {"id": id_, "section_id": fields["_section"]})
            elif fields.get("_project"):
                self._cmd("item_move", {"id": id_, "project_id": fields["_project"]})
        if "completed" in fields:
            self._cmd("item_close" if fields["completed"] else "item_uncomplete", {"id": id_})

    def add_section(self, project_id: str, name: str) -> str:
        temp_id = str(uuid.uuid4())
        self._cmd("section_add", {"name": name, "project_id": project_id}, temp_id)
        return temp_id

    def delete(self, id_: str) -> None:
        self._cmd("item_delete", {"id": id_})

    def pending(self) -> int:
        return len(self._queue)

    def queued(self) -> List[Dict[str, Any]]:
        return list(self._queue)

    def clear_queue(self) -> None:
        self._queue = []

    def flush(self) -> Tuple[Dict[str, str], Dict[str, str]]:
        """Send queued commands. Returns (temp_id -> real id, uuid -> error message)."""
        mapping: Dict[str, str] = {}
        errors: Dict[str, str] = {}
        while self._queue:
            batch, self._queue = self._queue[:MAX_COMMANDS], self._queue[MAX_COMMANDS:]
            data = self._post({
                "commands": batch,
                "sync_token": self.sync_token,
                "resource_types": RESOURCES,
            })
            for cmd_uuid, status in (data.get("sync_status") or {}).items():
                if status != "ok":
                    errors[cmd_uuid] = json.dumps(status, ensure_ascii=False)
            mapping.update({k: str(v) for k, v in (data.get("temp_id_mapping") or {}).items()})
            # Later commands in the queue may still reference temp ids from this batch.
            for cmd in self._queue:
                for key in ("id", "section_id", "project_id", "item_id", "parent_id"):
                    if cmd["args"].get(key) in mapping:
                        cmd["args"][key] = mapping[cmd["args"][key]]
            self._absorb(data)
        self._persist()
        return mapping, errors
