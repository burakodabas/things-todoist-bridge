"""Things 3 adapter.

Things keeps its database inside a protected group container, so instead of
reading SQLite we talk to Things through JavaScript for Automation (JXA).
Bulk property reads are fast (~0.2 s per list). Writes go through JXA too;
clearing dates is the one thing the scripting dictionary can't do, so that
uses the Things URL scheme when an auth token is configured.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional

from .models import Item

log = logging.getLogger("ttbridge.things")


class ThingsError(RuntimeError):
    pass


_COMMON_JS = r"""
const T = Application("Things3");
function ymd(d) {
  if (!d) return null;
  return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" +
         String(d.getDate()).padStart(2, "0");
}
function parseYmd(s) { const p = s.split("-").map(Number); return new Date(p[0], p[1] - 1, p[2]); }
function bulk(coll) {
  const n = coll.length;
  if (!n) return [];
  const id = coll.id(), name = coll.name(), notes = coll.notes(), st = coll.status(),
        tg = coll.tagNames(), due = coll.dueDate(), act = coll.activationDate(),
        mod = coll.modificationDate();
  const out = [];
  for (let i = 0; i < id.length; i++) {
    out.push({id: id[i], name: name[i], notes: notes[i], status: st[i], tags: tg[i],
              deadline: ymd(due[i]), when: ymd(act[i]), mod: mod[i] ? mod[i].getTime() / 1000 : 0});
  }
  return out;
}
function safe(f) { try { return f(); } catch (e) { return null; } }
"""

_READ_SCOPE_JS = _COMMON_JS + r"""
function run(argv) {
  const a = JSON.parse(argv[0]);
  let coll;
  if (a.type === "project") coll = T.projects.byId(a.id).toDos;
  else if (a.type === "area") coll = T.areas.byId(a.id).toDos;
  else if (a.type === "tag") coll = T.tags.byName(a.name).toDos;
  else throw new Error("unknown scope " + a.type);
  let items = bulk(coll);
  if (a.type === "tag") {
    const projects = new Set(T.projects.id());
    items = items.filter(x => !projects.has(x.id));
  }
  return JSON.stringify(items);
}
"""

_READ_IDS_JS = _COMMON_JS + r"""
function run(argv) {
  const ids = JSON.parse(argv[0]);
  const trash = new Set(T.lists.byName("Trash").toDos.id());
  const out = {};
  for (const id of ids) {
    if (trash.has(id)) { out[id] = {missing: true, trashed: true}; continue; }
    const o = T.toDos.byId(id);
    const name = safe(() => o.name());
    if (name === null) { out[id] = {missing: true}; continue; }
    const due = safe(() => o.dueDate()), act = safe(() => o.activationDate()), mod = safe(() => o.modificationDate());
    out[id] = {id: id, name: name, notes: safe(() => o.notes()) || "", status: o.status(),
               tags: safe(() => o.tagNames()) || "", deadline: ymd(due), when: ymd(act),
               mod: mod ? mod.getTime() / 1000 : 0,
               project: safe(() => o.project().id()), area: safe(() => o.area().id())};
  }
  return JSON.stringify(out);
}
"""

_META_JS = _COMMON_JS + r"""
function pairs(coll) { const n = coll.name(); return coll.id().map((id, i) => ({id: id, name: n[i]})); }
function run() {
  return JSON.stringify({projects: pairs(T.projects), areas: pairs(T.areas), tags: T.tags.name()});
}
"""

_WRITE_JS = _COMMON_JS + r"""
function ensureTags(names) {
  const have = new Set(T.tags.name());
  for (const n of names) if (!have.has(n)) { T.tags.push(T.Tag({name: n})); have.add(n); }
}
function apply(o, f) {
  const needsUrl = {};
  if ("title" in f) o.name = f.title;
  if ("notes" in f) o.notes = f.notes;
  if ("tags" in f) { ensureTags(f.tags); o.tagNames = f.tags.join(", "); }
  if ("deadline" in f) { if (f.deadline) o.dueDate = parseYmd(f.deadline); else needsUrl.deadline = ""; }
  if ("when" in f) { if (f.when) T.schedule(o, {for: parseYmd(f.when)}); else needsUrl.when = ""; }
  if ("completed" in f) o.status = f.completed ? "completed" : "open";
  return needsUrl;
}
function run(argv) {
  const ops = JSON.parse(argv[0]);
  const results = [];
  for (const op of ops) {
    try {
      if (op.op === "create") {
        const t = T.ToDo({name: op.fields.title || ""});
        T.lists.byName("Inbox").toDos.push(t);
        const id = t.id();
        const o = T.toDos.byId(id);
        if (op.project) o.project = T.projects.byId(op.project);
        else if (op.area) o.area = T.areas.byId(op.area);
        const f = Object.assign({}, op.fields); delete f.title;
        if (!f.when) delete f.when; if (!f.deadline) delete f.deadline; if (!f.completed) delete f.completed;
        apply(o, f);
        results.push({ok: true, id: id});
      } else if (op.op === "update") {
        const o = T.toDos.byId(op.id);
        results.push({ok: true, id: op.id, url: apply(o, op.fields)});
      } else if (op.op === "delete") {
        T.delete(T.toDos.byId(op.id));
        results.push({ok: true, id: op.id});
      } else {
        results.push({ok: false, error: "unknown op " + op.op});
      }
    } catch (e) {
      results.push({ok: false, id: op.id || null, error: String(e)});
    }
  }
  return JSON.stringify(results);
}
"""


def _osascript(script: str, arg: Any = None, timeout: int = 120) -> Any:
    cmd = ["osascript", "-l", "JavaScript", "-e", script]
    if arg is not None:
        cmd.append(json.dumps(arg, ensure_ascii=False))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ThingsError("Things did not respond in time") from exc
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            err = ("macOS blocked automation access to Things. Allow it in System Settings > "
                   "Privacy & Security > Automation. (" + err + ")")
        raise ThingsError(err)
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _item(d: Dict[str, Any]) -> Item:
    return Item(
        id=d["id"],
        title=d.get("name") or "",
        notes=d.get("notes") or "",
        when=d.get("when"),
        deadline=d.get("deadline"),
        tags=d.get("tags") or "",
        completed=d.get("status") in ("completed", "canceled"),
        modified=float(d.get("mod") or 0),
        project=d.get("project"),
        area=d.get("area"),
        extra={"status": d.get("status")},
    )


class ThingsClient:
    def __init__(self, auth_token: str = "", launch: bool = True):
        self.auth_token = auth_token
        self.launch = launch

    # ---- reads -----------------------------------------------------------
    def is_running(self) -> bool:
        try:
            out = subprocess.run(["pgrep", "-x", "Things3"], capture_output=True)
            return out.returncode == 0
        except OSError:
            return False

    def ensure_available(self) -> None:
        if not self.launch and not self.is_running():
            raise ThingsError("Things 3 is not running (auto-launch disabled)")

    def meta(self) -> Dict[str, Any]:
        self.ensure_available()
        return _osascript(_META_JS)

    def scope_items(self, scope: Dict[str, Any]) -> Dict[str, Item]:
        """Items currently in a project/area/tag (Things returns open and recently completed)."""
        self.ensure_available()
        rows = _osascript(_READ_SCOPE_JS, scope)
        items = {}
        for row in rows:
            it = _item(row)
            if scope["type"] == "project":
                it.project = scope["id"]
            elif scope["type"] == "area":
                it.area = scope["id"]
            items[it.id] = it
        return items

    def get_items(self, ids: Iterable[str]) -> Dict[str, Optional[Item]]:
        """Look up specific items. Missing or trashed items map to an Item with deleted=True."""
        ids = list(ids)
        if not ids:
            return {}
        self.ensure_available()
        rows = _osascript(_READ_IDS_JS, ids)
        out: Dict[str, Optional[Item]] = {}
        for id_, row in rows.items():
            if row.get("missing"):
                out[id_] = Item(id=id_, title="", deleted=True)
            else:
                out[id_] = _item(row)
        return out

    # ---- writes ----------------------------------------------------------
    def run_ops(self, ops: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not ops:
            return []
        self.ensure_available()
        results = _osascript(_WRITE_JS, ops, timeout=300)
        for res in results:
            url = res.get("url") if res.get("ok") else None
            if url:
                self._clear_via_url(res["id"], url)
        return results

    def _clear_via_url(self, id_: str, params: Dict[str, str]) -> None:
        if not self.auth_token:
            log.warning("Cannot clear %s on Things item %s: set the Things auth token in settings",
                        ", ".join(params), id_)
            return
        query = {"id": id_, "auth-token": self.auth_token}
        query.update(params)
        url = "things:///update?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        subprocess.run(["open", "-g", url], check=False)
