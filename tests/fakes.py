"""In-memory fakes for Things and the Todoist Sync API."""

from __future__ import annotations

import io
import itertools
import json
import time
import urllib.parse
from typing import Any, Dict, List

from ttbridge.models import Item, norm_tags

_clock = itertools.count(1_000_000)


def tick() -> float:
    return float(next(_clock))


class FakeThings:
    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {}
        self.trash: set = set()
        self.projects: Dict[str, Dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self.fail_next_update = False
        self.extra_tags = []

    def add_project(self, name, area=None) -> str:
        id_ = f"TPR{next(self._ids)}"
        self.projects[id_] = {"id": id_, "name": name, "area": area, "status": "open"}
        return id_

    def add(self, title, project=None, area=None, tags=(), **kw) -> str:
        id_ = f"T{next(self._ids)}"
        self.items[id_] = dict(id=id_, title=title, notes=kw.get("notes", ""), when=kw.get("when"),
                               deadline=kw.get("deadline"), tags=list(tags), completed=kw.get("completed", False),
                               project=project, area=area, modified=tick())
        return id_

    def edit(self, id_, **kw):
        self.items[id_].update(kw)
        self.items[id_]["modified"] = tick()

    def _item(self, d) -> Item:
        return Item(id=d["id"], title=d["title"], notes=d["notes"], when=d["when"], deadline=d["deadline"],
                    tags=d["tags"], completed=d["completed"], modified=d["modified"],
                    project=d["project"], area=d["area"])

    def meta(self):
        tags = sorted({t for d in self.items.values() for t in d["tags"]} | set(self.extra_tags))
        return {"projects": [], "areas": [], "tags": tags}

    def scope_items(self, scope):
        return self.scope(scope)[0]

    def scope(self, scope):
        out = {}
        area_projects = [p for p in self.projects.values()
                         if scope["type"] == "area" and p["area"] == scope["id"] and p["status"] == "open"]
        in_area = {p["id"] for p in area_projects}
        for d in self.items.values():
            if d["id"] in self.trash:
                continue
            if scope["type"] == "project" and d["project"] != scope["id"]:
                continue
            if scope["type"] == "area" and d["area"] != scope["id"] and d["project"] not in in_area:
                continue
            if scope["type"] == "tag" and scope["name"] not in d["tags"]:
                continue
            out[d["id"]] = self._item(d)
        return out, [{"id": p["id"], "name": p["name"]} for p in area_projects]

    def get_items(self, ids):
        out = {}
        for id_ in ids:
            if id_ in self.projects:
                out[id_] = Item(id=id_, title=self.projects[id_]["name"], extra={"is_project": True})
            elif id_ in self.trash or id_ not in self.items:
                out[id_] = Item(id=id_, title="", deleted=True)
            else:
                out[id_] = self._item(self.items[id_])
        return out

    def _place(self, fields, refs):
        f = dict(fields)
        if f.get("project"):
            p = f.pop("project")
            f["project"] = refs.get(p[1:]) if p.startswith("@") else p
            f["area"] = None
        elif f.get("area"):
            f["project"] = None
        return f

    def run_ops(self, ops):
        results = []
        refs = {}
        for op in ops:
            if op["op"] == "create_project":
                id_ = self.add_project(op["name"], op.get("area"))
                refs[op["ref"]] = id_
                results.append({"ok": True, "id": id_, "ref": op["ref"]})
            elif op["op"] == "create":
                f = op["fields"]
                project = op.get("project")
                if project and project.startswith("@"):
                    project = refs[project[1:]]
                id_ = self.add(f.get("title", ""), project=project, area=None if project else op.get("area"),
                               tags=f.get("tags", []), notes=f.get("notes", ""), when=f.get("when"),
                               deadline=f.get("deadline"), completed=f.get("completed", False))
                results.append({"ok": True, "id": id_})
            elif op["op"] == "update":
                if self.fail_next_update:
                    self.fail_next_update = False
                    results.append({"ok": False, "id": op["id"], "error": "boom"})
                    continue
                self.edit(op["id"], **self._place(op["fields"], refs))
                results.append({"ok": True, "id": op["id"]})
            elif op["op"] == "delete":
                self.trash.add(op["id"])
                results.append({"ok": True, "id": op["id"]})
        return results


class FakeTodoistServer:
    """Implements the subset of POST /api/v1/sync the client uses."""

    def __init__(self):
        self.items: Dict[str, Dict[str, Any]] = {}
        self.projects = {"P_inbox": {"id": "P_inbox", "name": "Inbox", "inbox_project": True},
                         "P_work": {"id": "P_work", "name": "Work"}}
        self.labels = {"L1": {"id": "L1", "name": "Home"}}
        self.sections: Dict[str, Dict[str, Any]] = {}
        self.notes: Dict[str, Dict[str, Any]] = {}
        self.user = {"id": "U_me", "full_name": "Me"}
        self.collaborators = {"U_mert": {"id": "U_mert", "full_name": "Mert", "email": "mert@example.com"}}
        self.collaborator_states = [{"project_id": "P_work", "user_id": "U_me", "state": "active"},
                                    {"project_id": "P_work", "user_id": "U_mert", "state": "active"}]
        self.version = 0
        self.changed_at: Dict[str, int] = {}
        self._ids = itertools.count(100)
        self.reject = set()  # command types to reject
        self.reject_notes = False
        self.requests = 0

    # helpers for tests
    def add(self, content, project_id="P_work", labels=(), **kw) -> str:
        id_ = str(next(self._ids))
        self.items[id_] = {"id": id_, "content": content, "description": kw.get("description", ""),
                           "project_id": project_id, "labels": list(labels), "due": kw.get("due"),
                           "deadline": kw.get("deadline"), "checked": False, "is_deleted": False,
                           "updated_at": _iso(tick()), "completed_at": None,
                           "section_id": kw.get("section_id"), "responsible_uid": kw.get("responsible_uid")}
        self._touch(id_)
        return id_

    def edit(self, id_, **kw):
        self.items[id_].update(kw)
        self.items[id_]["updated_at"] = _iso(tick())
        self._touch(id_)

    def add_section(self, name, project_id="P_work") -> str:
        id_ = "S" + str(next(self._ids))
        self.sections[id_] = {"id": id_, "name": name, "project_id": project_id, "is_deleted": False}
        return id_

    def comment(self, item_id, content, uid="U_mert", posted_at="2026-11-07T10:00:00Z") -> str:
        id_ = "N" + str(next(self._ids))
        self.notes[id_] = {"id": id_, "item_id": item_id, "content": content, "posted_uid": uid,
                           "posted_at": posted_at, "is_deleted": False}
        self._touch(item_id)
        return id_

    def _touch(self, id_):
        self.version += 1
        self.changed_at[id_] = self.version

    # transport
    def __call__(self, req, timeout=None):
        self.requests += 1
        form = {k: v[0] for k, v in urllib.parse.parse_qs(req.data.decode()).items()}
        status, mapping = {}, {}
        for cmd in json.loads(form.get("commands", "[]")):
            args = dict(cmd["args"])
            if cmd["type"] in self.reject:
                status[cmd["uuid"]] = {"error": "rejected", "error_code": 1}
                continue
            for key in ("id", "section_id"):
                if key in args and args[key] in mapping:
                    args[key] = mapping[args[key]]
            t = cmd["type"]
            if t == "item_add":
                id_ = self.add(args["content"], args.get("project_id", "P_inbox"), args.get("labels", []),
                               description=args.get("description", ""), due=args.get("due"),
                               deadline=args.get("deadline"), section_id=args.get("section_id"),
                               responsible_uid=args.get("responsible_uid"))
                mapping[cmd["temp_id"]] = id_
            elif t == "note_add":
                if self.reject_notes:
                    status[cmd["uuid"]] = {"error": "rejected", "error_code": 1}
                    continue
                nid = self.comment(args["item_id"], args["content"], uid=self.user["id"],
                                   posted_at="2026-11-07T12:00:00Z")
                self.notes[nid]["uids_to_notify"] = args.get("uids_to_notify", [])
                mapping[cmd["temp_id"]] = nid
            elif t == "section_add":
                mapping[cmd["temp_id"]] = self.add_section(args["name"], args["project_id"])
            elif t == "item_move":
                id_ = args.pop("id")
                if "section_id" in args:
                    self.edit(id_, section_id=args["section_id"],
                              project_id=self.sections[args["section_id"]]["project_id"])
                else:
                    self.edit(id_, section_id=None, project_id=args["project_id"])
            elif t == "item_update":
                id_ = args.pop("id")
                self.edit(id_, **args)
            elif t == "item_close":
                self.edit(args["id"], checked=True, completed_at=_iso(tick()))
            elif t == "item_uncomplete":
                self.edit(args["id"], checked=False, completed_at=None)
            elif t == "item_delete":
                self.edit(args["id"], is_deleted=True)
            status[cmd["uuid"]] = "ok"
        token = form.get("sync_token", "*")
        full = token == "*"
        since = 0 if full else int(token)
        if full:
            items = [i for i in self.items.values() if not i["checked"] and not i["is_deleted"]]
        else:
            items = [self.items[i] for i, v in self.changed_at.items() if v > since]
        body = {"sync_token": str(self.version), "full_sync": full, "items": json.loads(json.dumps(items)),
                "projects": list(self.projects.values()) if full else [],
                "labels": list(self.labels.values()) if full else [],
                "sections": list(self.sections.values()), "notes": list(self.notes.values()),
                "collaborators": list(self.collaborators.values()),
                "collaborator_states": self.collaborator_states, "user": self.user,
                "sync_status": status, "temp_id_mapping": mapping}
        return _Resp(json.dumps(body).encode())


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
