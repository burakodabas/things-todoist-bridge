"""The sync engine: a three-way merge between Things, Todoist and the last synced snapshot.

For every linked pair the engine compares the current Things values and the
current Todoist values against the snapshot taken at the last successful
sync. A field that changed on only one side is copied to the other side; a
field that changed on both sides is resolved by modification time.

Snapshots are only written after the writes they describe succeeded, so a
rejected change is retried on the next cycle instead of being reverted.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .models import Item, norm_tags

log = logging.getLogger("ttbridge.engine")

Event = Dict[str, Any]


@dataclass
class RuleReport:
    rule_id: str
    name: str
    created_in_todoist: int = 0
    created_in_things: int = 0
    updated_todoist: int = 0
    updated_things: int = 0
    deleted: int = 0
    unlinked: int = 0
    matched: int = 0
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class PendingSnapshot:
    """A link snapshot to write once the related writes succeeded."""

    rule_id: str
    things_id: str
    todoist_id: str  # may be a temp id until the Todoist flush resolves it
    snapshot: Dict[str, Any]
    new_link: bool = False
    temp_id: bool = False
    things_ok: bool = True
    todoist_uuids: List[str] = field(default_factory=list)
    fallback_snapshot: Optional[Dict[str, Any]] = None  # used for new links if a write failed


def rule_fields(rule: Dict[str, Any]) -> Tuple[str, ...]:
    fields = ["title"]
    if rule.get("sync_notes", True):
        fields.append("notes")
    if rule.get("sync_when", True):
        fields.append("when")
    if rule.get("sync_deadline", True):
        fields.append("deadline")
    if rule.get("sync_tags", True):
        fields.append("tags")
    fields.append("completed")
    if rule.get("assignees") or rule.get("auto_assign", True):
        fields.append("assignee")
    if rule.get("things", {}).get("type") == "area" and rule.get("area_projects", "sections") == "sections":
        fields.append("group")
    return tuple(fields)


# ---- people ------------------------------------------------------------------
_MENTION = re.compile(r"(?:^|(?<=\s))[@+]([^\s@+#,;:!?()]+)")


def person_key(name: str) -> str:
    return "".join((name or "").strip().lstrip("@#+").split()).casefold()


def mention_in(title: str, people: Dict[str, str]) -> Tuple[str, str]:
    """(title without "@name" mentions of known people, the first mentioned user id or "")."""
    found = ""

    def drop(m):
        nonlocal found
        uid = people.get(person_key(m.group(1).rstrip(".")))
        if not uid:
            return m.group(0)
        found = found or uid
        return ""

    cleaned = _MENTION.sub(drop, title or "")
    return (" ".join(cleaned.split()) if found else title), found


# ---- Todoist comments shown in the Things notes ---------------------------
COMMENTS_MARK = "── Todoist comments ──"


def split_notes(notes: str) -> Tuple[str, str]:
    """(the user's own notes, the comments block) of a Things note."""
    notes = notes or ""
    i = notes.find(COMMENTS_MARK)
    if i < 0:
        return notes.strip(), ""
    return notes[:i].strip(), notes[i:].strip()


def join_notes(own: str, block: str) -> str:
    own, block = (own or "").strip(), (block or "").strip()
    return f"{own}\n\n{block}" if own and block else (own or block)


def render_comments(comments: List[Dict[str, Any]], name_of: Callable[[Any], str]) -> str:
    lines = []
    for c in comments:
        text = (c.get("content") or "").strip()
        if not text and c.get("file_attachment"):
            att = c["file_attachment"]
            text = f"[{att.get('file_name') or 'attachment'}] {att.get('file_url') or ''}".strip()
        if not text:
            continue
        when = ""
        if c.get("posted_at"):
            try:
                dt = datetime.fromisoformat(c["posted_at"].replace("Z", "+00:00")).astimezone()
                when = dt.strftime("%Y-%m-%d %H:%M") + " · "
            except ValueError:
                pass
        lines.append(f"{when}{name_of(c.get('posted_uid'))}: {text}")
    return COMMENTS_MARK + "\n" + "\n".join(lines) if lines else ""


def _eq(field_name: str, a: Any, b: Any) -> bool:
    if field_name == "tags":
        return list(norm_tags(a or [])) == list(norm_tags(b or []))
    if field_name in ("when", "deadline"):
        return (a or None) == (b or None)
    if field_name == "completed":
        return bool(a) == bool(b)
    return (a or "") == (b or "")


def merge(fields: Tuple[str, ...], tf: Dict[str, Any], df: Dict[str, Any], snap: Dict[str, Any],
          t_mod: float, d_mod: float, direction: str) -> Dict[str, Any]:
    """Merged value for every field (see module docstring)."""
    allow_t2d = direction in ("both", "things_to_todoist")
    allow_d2t = direction in ("both", "todoist_to_things")
    out = {}
    for f in fields:
        tv, dv = tf.get(f), df.get(f)
        if _eq(f, tv, dv):
            out[f] = tv
        elif not allow_d2t:
            out[f] = tv
        elif not allow_t2d:
            out[f] = dv
        else:
            t_changed = f not in snap or not _eq(f, tv, snap.get(f))
            d_changed = f not in snap or not _eq(f, dv, snap.get(f))
            if t_changed and not d_changed:
                out[f] = tv
            elif d_changed and not t_changed:
                out[f] = dv
            else:
                out[f] = tv if t_mod >= d_mod else dv
    return out


class SyncEngine:
    def __init__(self, things, todoist, state, on_event: Optional[Callable[[Event], None]] = None):
        self.things = things
        self.todoist = todoist
        self.state = state
        self.on_event = on_event or (lambda e: None)

    def _event(self, rule: Dict[str, Any], action: str, title: str, detail: str = "") -> None:
        self.on_event({"ts": time.time(), "rule": rule.get("name"), "action": action,
                       "title": title, "detail": detail})

    def _todoist(self, rule: Dict[str, Any], title: str, fn: Callable[[], Any]) -> Tuple[Any, List[str]]:
        """Run a Todoist queue call and remember which commands it produced."""
        before = self.todoist.pending()
        result = fn()
        uuids = [c["uuid"] for c in self.todoist.queued()[before:]]
        for u in uuids:
            self._cmd_context[u] = (rule, title)
        return result, uuids

    # ------------------------------------------------------------------
    def run(self, rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        self.todoist.refresh()
        self._claimed_things: Set[str] = set()
        self._claimed_todoist: Set[str] = set()
        self._pending: List[PendingSnapshot] = []
        self._cmd_context: Dict[str, Tuple[Dict[str, Any], str]] = {}
        self._new_groups: List[Tuple[str, str, str, str, List[str]]] = []
        self._tags_cache: Optional[List[str]] = None
        reports: List[RuleReport] = []
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            report = RuleReport(rule["id"], rule.get("name", ""))
            try:
                self._sync_rule(rule, report)
            except Exception as exc:  # keep other rules running
                log.exception("Rule %s failed", rule.get("name"))
                report.errors.append(str(exc))
                self._event(rule, "error", "", str(exc))
            reports.append(report)
        self._finish(reports)
        return [r.as_dict() for r in reports]

    def _finish(self, reports: List[RuleReport]) -> None:
        mapping: Dict[str, str] = {}
        errors: Dict[str, str] = {}
        if self.todoist.pending():
            try:
                mapping, errors = self.todoist.flush()
            except Exception as exc:
                log.exception("Todoist flush failed")
                errors = {c["uuid"]: str(exc) for c in self.todoist.queued()}
                self.todoist.clear_queue()
        by_rule = {r.rule_id: r for r in reports}
        for cmd_uuid, err in errors.items():
            rule, title = self._cmd_context.get(cmd_uuid, ({"name": "?", "id": ""}, ""))
            msg = f"Todoist rejected change for '{title}': {err}"
            log.error(msg)
            if rule.get("id") in by_rule:
                by_rule[rule["id"]].errors.append(msg)
            self._event(rule, "error", title, err)

        for rule_id, things_project, temp, name, uuids in self._new_groups:
            if temp in mapping and not any(u in errors for u in uuids):
                self.state.add_group(rule_id, things_project, mapping[temp], name)

        for p in self._pending:
            ok = p.things_ok and not any(u in errors for u in p.todoist_uuids)
            todoist_id = mapping.get(p.todoist_id, p.todoist_id)
            if p.new_link:
                if p.temp_id and p.todoist_id not in mapping:
                    continue  # item_add failed: the task was never created
                snapshot = p.snapshot if ok else (p.fallback_snapshot or {})
                self.state.add_link(p.rule_id, p.things_id, todoist_id, snapshot)
            elif ok:
                self.state.update_snapshot(p.things_id, p.snapshot)

    # ------------------------------------------------------------------
    def _sync_rule(self, rule: Dict[str, Any], report: RuleReport) -> None:
        fields = rule_fields(rule)
        direction = rule.get("direction", "both")
        allow_t2d = direction in ("both", "things_to_todoist")
        allow_d2t = direction in ("both", "todoist_to_things")
        tscope, dscope = rule["things"], rule["todoist"]
        t_scope_tag = (tscope["name"],) if tscope["type"] == "tag" else ()
        d_scope_label = (dscope["name"],) if dscope["type"] == "label" else ()
        sync_comments = rule.get("sync_comments", True) and allow_d2t

        # Where new Todoist tasks of this rule go (and the project whose sections mirror Things projects).
        if dscope["type"] == "project":
            target_project = dscope["id"]
        else:
            target_project = dscope.get("create_project_id") or self.todoist.inbox_id()

        if hasattr(self.things, "scope"):
            t_items, t_projects = self.things.scope(tscope)
        else:
            t_items, t_projects = self.things.scope_items(tscope), []
        d_items = self.todoist.scope_items(dscope)
        links = self.state.links(rule["id"])

        missing = [l["things_id"] for l in links if l["things_id"] not in t_items]
        t_extra = self.things.get_items(missing) if missing else {}

        things_ops: List[Dict[str, Any]] = []
        op_owner: List[Optional[PendingSnapshot]] = []
        project_ops: List[Dict[str, Any]] = []  # Things projects to create first (from Todoist sections)
        created_in_things: List[Tuple[int, Item, Dict[str, Any]]] = []

        # ---- assignees: Things tag / @mention <-> Todoist responsible user ----
        # The engine value of "assignee" is a Todoist user id ("" = nobody or me, "~uid" = a
        # collaborator Things has no tag for).
        my_uid = self.todoist.my_uid() if hasattr(self.todoist, "my_uid") else ""
        people, person_tag = self._people(rule, target_project, my_uid)

        def is_person_tag(tag: str) -> bool:
            return person_key(tag) in people

        def t_assignee(t: Item) -> str:
            for tag in t.tags:
                if is_person_tag(tag):
                    return people[person_key(tag)]
            return mention_in(t.title, people)[1]

        def d_assignee(d: Item) -> str:
            uid = d.extra.get("responsible_uid")
            if not uid or uid == my_uid:
                return mention_in(d.title, people)[1]
            return uid if uid in person_tag else "~" + uid

        # ---- groups: Things projects in the area <-> Todoist sections ----------
        area_mode = "group" in fields
        open_projects = {p["id"]: p["name"] for p in t_projects}
        t2s: Dict[str, str] = {}
        s2t: Dict[str, str] = {}
        new_groups: List[Tuple[str, str, str, List[str]]] = []  # (things project, temp section, name, uuids)
        project_refs: Dict[str, str] = {}  # Todoist section id -> "@ref" of a Things project created now
        if area_mode:
            sections = self.todoist.sections_of(target_project)
            for g in self.state.groups(rule["id"]):
                if g["things_id"] in open_projects and g["todoist_id"] in sections:
                    t2s[g["things_id"]], s2t[g["todoist_id"]] = g["todoist_id"], g["things_id"]
                else:
                    self.state.remove_group(rule["id"], g["things_id"])
            free = {s.get("name", "").casefold(): sid for sid, s in sections.items() if sid not in s2t}
            for pid, name in open_projects.items():
                if pid in t2s:
                    continue
                sid = free.pop(name.casefold(), None)
                if sid:
                    self.state.add_group(rule["id"], pid, sid, name)
                    t2s[pid], s2t[sid] = sid, pid
                elif allow_t2d:
                    temp, uuids = self._todoist(rule, name, lambda: self.todoist.add_section(target_project, name))
                    t2s[pid] = temp
                    new_groups.append((pid, temp, name, uuids))
                    self._event(rule, "created section in Todoist", name)

        def t_group(t: Item) -> str:
            return t.project if t.project in open_projects else ""

        def d_group(d: Item, t: Optional[Item]) -> str:
            if str(d.project) != str(target_project):
                return t_group(t) if t is not None else ""  # lives elsewhere (label rule): don't move it
            sid = d.extra.get("section_id")
            if not sid:
                return ""
            return s2t.get(sid, "~" + sid)  # "~sid": a section with no Things project yet

        def things_project_for(sid: str) -> str:
            if sid not in project_refs:
                ref = f"p{len(project_ops)}"
                name = (self.todoist.sections.get(sid) or {}).get("name") or "Todoist section"
                project_ops.append({"op": "create_project", "ref": ref, "name": name, "area": tscope["id"],
                                    "_section": sid})
                project_refs[sid] = "@" + ref
                self._event(rule, "created project in Things", name)
            return project_refs[sid]

        # ---- field views ------------------------------------------------
        def tfields(t: Item, snap: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
            f = t.fields(t_scope_tag + tuple(x for x in t.tags if is_person_tag(x)))
            f["title"] = mention_in(t.title, people)[0]
            f["notes"] = split_notes(f["notes"])[0]
            f["assignee"] = t_assignee(t)
            if not f["assignee"] and snap and str(snap.get("assignee", "")).startswith("~"):
                f["assignee"] = snap["assignee"]  # assigned to someone Things can't show: keep it
            f["group"] = t_group(t)
            return {k: v for k, v in f.items() if k in fields}

        def dfields(d: Item, t: Optional[Item] = None) -> Dict[str, Any]:
            f = d.fields(d_scope_label + tuple(x for x in d.tags if is_person_tag(x)))
            f["title"] = mention_in(d.title, people)[0]
            f["assignee"] = d_assignee(d)
            f["group"] = d_group(d, t)
            return {k: v for k, v in f.items() if k in fields}

        def comments_block(d: Item) -> str:
            if not sync_comments:
                return ""
            return render_comments(self.todoist.comments(d.id), self.todoist.user_name)

        def for_things(values: Dict[str, Any], t: Optional[Item], block: Optional[str]) -> Dict[str, Any]:
            """Engine values -> Things write fields."""
            out = {k: values[k] for k in ("title", "when", "deadline", "completed") if k in values}
            if "notes" in values or block is not None:
                own = values["notes"] if "notes" in values else split_notes(t.notes if t else "")[0]
                current_block = split_notes(t.notes)[1] if t else ""
                out["notes"] = join_notes(own, current_block if block is None else block)
            if "tags" in values or "assignee" in values or t is None:
                tags = values["tags"] if "tags" in values else (
                    [x for x in t.tags if not is_person_tag(x) and x not in t_scope_tag] if t else [])
                who = values["assignee"] if "assignee" in values else (t_assignee(t) if t else "")
                extra = [person_tag[who]] if who in person_tag else []
                out["tags"] = list(norm_tags(list(tags) + list(t_scope_tag) + extra))
            if "group" in values:
                g = values["group"]
                if g.startswith("~"):
                    out["project"] = things_project_for(g[1:])
                elif g:
                    out["project"] = g
                else:
                    out["area"] = tscope["id"]
            return out

        def for_todoist(values: Dict[str, Any], creating: bool = False) -> Dict[str, Any]:
            """Engine values -> Todoist write fields."""
            out = {k: values[k] for k in ("title", "notes", "when", "deadline", "completed") if k in values}
            if "tags" in values or creating:
                out["tags"] = list(norm_tags(list(values.get("tags", [])) + list(d_scope_label)))
            if "assignee" in values and not str(values["assignee"]).startswith("~"):
                out["_responsible"] = values["assignee"] or None
            if "group" in values:
                g = values["group"]
                if g.startswith("~"):
                    out["_section"] = g[1:]
                elif g and g in t2s:
                    out["_section"] = t2s[g]
                elif not g:
                    out["_project"] = target_project
            return out

        def apply_merge(t: Item, d: Item, snap: Dict[str, Any], pending: PendingSnapshot) -> None:
            tf, df = tfields(t, snap), dfields(d, t)
            result = merge(fields, tf, df, snap, t.modified, d.modified, direction)
            if d.recurring and "when" in result and not _eq("when", result["when"], df.get("when")):
                result["when"] = df.get("when")  # recurring due dates are owned by Todoist
            t_patch = {f: result[f] for f in fields if allow_d2t and not _eq(f, result[f], tf.get(f))}
            d_patch = {f: result[f] for f in fields if allow_t2d and not _eq(f, result[f], df.get(f))}
            if "assignee" in t_patch and str(t_patch["assignee"]).startswith("~") and not tf.get("assignee"):
                del t_patch["assignee"]  # nothing to show in Things
            # An "@name" typed into a title becomes a real assignment: clean the title on both sides.
            if allow_d2t and "title" in fields and t.title != tf.get("title") and "title" not in t_patch:
                t_patch["title"] = result["title"]
                if "assignee" in fields and result.get("assignee") in person_tag:
                    t_patch["assignee"] = result["assignee"]
            if allow_t2d and "title" in fields and d.title != df.get("title") and "title" not in d_patch:
                d_patch["title"] = result["title"]
                if "assignee" in fields and result.get("assignee") and not str(result["assignee"]).startswith("~"):
                    d_patch["assignee"] = result["assignee"]
            block = comments_block(d) if sync_comments else None
            comments_changed = block is not None and block != split_notes(t.notes)[1]
            title = result.get("title") or t.title
            if t_patch or comments_changed:
                things_ops.append({"op": "update", "id": t.id,
                                   "fields": for_things(t_patch, t, block if comments_changed else None)})
                op_owner.append(pending)
                report.updated_things += 1
                what = list(t_patch) + (["comments"] if comments_changed else [])
                self._event(rule, "updated in Things", title, ", ".join(what))
            if d_patch:
                _, uuids = self._todoist(rule, title, lambda: self.todoist.update(d.id, for_todoist(d_patch)))
                pending.todoist_uuids.extend(uuids)
                report.updated_todoist += 1
                self._event(rule, "updated in Todoist", title, ", ".join(d_patch))
            pending.snapshot = result
            if t_patch or d_patch or pending.new_link or result != snap:
                self._pending.append(pending)

        # ---- existing links ---------------------------------------------
        for link in links:
            tid, did = link["things_id"], link["todoist_id"]
            snap = link["snapshot"]
            title = link.get("title") or snap.get("title") or ""
            self._claimed_things.add(tid)
            self._claimed_todoist.add(did)

            t = t_items.get(tid) or t_extra.get(tid)
            if t is not None and t.extra.get("is_project"):
                # Older versions synced the projects of an area as tasks.
                self.state.remove_link(tid)
                self.state.ignore(rule["id"], "todoist", did)
                report.unlinked += 1
                self._event(rule, "unlinked", title, "this is a Things project, not a to-do")
                continue
            t_gone = t is None or t.deleted
            t_out = (not t_gone) and tid not in t_items and not t.completed

            d = self.todoist.get_item(did)
            if d is None:
                # Not in the cache (e.g. completed while the cache was rebuilt): treat as completed.
                d = Item(id=did, title=snap.get("title", ""), notes=snap.get("notes", ""),
                         when=snap.get("when"), deadline=snap.get("deadline"),
                         tags=snap.get("tags", []), completed=True, modified=0.0,
                         project=target_project, extra={"responsible_uid": None})
                d.extra["_assumed"] = True
                who = str(snap.get("assignee") or "")
                d.extra["responsible_uid"] = who.lstrip("~") or None
            d_gone = d.deleted
            d_out = (not d_gone) and did not in d_items and not d.completed

            if t_gone or d_gone:
                if t_gone and d_gone:
                    pass
                elif rule.get("on_delete") == "delete" and t_gone and allow_t2d:
                    self._todoist(rule, title, lambda: self.todoist.delete(did))
                    self._event(rule, "deleted in Todoist", title)
                    report.deleted += 1
                elif rule.get("on_delete") == "delete" and d_gone and allow_d2t:
                    things_ops.append({"op": "delete", "id": tid})
                    op_owner.append(None)
                    self._event(rule, "deleted in Things", title)
                    report.deleted += 1
                else:
                    self._event(rule, "unlinked", title, "deleted in " + ("Things" if t_gone else "Todoist"))
                    report.unlinked += 1
                    # Keep the surviving copy out of the sync, or it would be re-created right away.
                    self.state.ignore(rule["id"], "todoist" if t_gone else "things", did if t_gone else tid)
                self.state.remove_link(tid)
                continue

            if t_out or d_out:
                self.state.remove_link(tid)
                report.unlinked += 1
                self._event(rule, "unlinked", title, "moved out of scope in " + ("Things" if t_out else "Todoist"))
                continue

            if d.extra.get("_assumed"):
                # We don't know its section/assignee any more: only let completion through.
                d.extra["section_id"] = None
                d.project = None
            apply_merge(t, d, snap, PendingSnapshot(rule["id"], tid, did, snap))

        # ---- unlinked items ---------------------------------------------
        linked_t = self.state.linked_things_ids() | self._claimed_things | self.state.ignored(rule["id"], "things")
        linked_d = self.state.linked_todoist_ids() | self._claimed_todoist | self.state.ignored(rule["id"], "todoist")
        include_done = rule.get("import_completed", False)
        new_t = [t for t in t_items.values() if t.id not in linked_t and (include_done or not t.completed)]
        new_d = [d for d in d_items.values() if d.id not in linked_d and (include_done or not d.completed)]

        # Pair identical titles first so the first run doesn't duplicate existing tasks.
        d_by_title: Dict[str, List[Item]] = {}
        for d in new_d:
            d_by_title.setdefault(d.title.casefold(), []).append(d)
        unmatched_t: List[Item] = []
        for t in new_t:
            bucket = d_by_title.get(t.title.casefold())
            if not bucket:
                unmatched_t.append(t)
                continue
            d = bucket.pop(0)
            new_d.remove(d)
            self._claimed_things.add(t.id)
            self._claimed_todoist.add(d.id)
            report.matched += 1
            self._event(rule, "linked existing", t.title)
            apply_merge(t, d, {}, PendingSnapshot(rule["id"], t.id, d.id, {}, new_link=True,
                                                  fallback_snapshot={}))

        if allow_t2d:
            for t in unmatched_t:
                values = tfields(t)
                temp_id, uuids = self._todoist(
                    rule, t.title, lambda: self.todoist.add(target_project, for_todoist(values, creating=True)))
                self._pending.append(PendingSnapshot(rule["id"], t.id, temp_id, values, new_link=True,
                                                     temp_id=True, todoist_uuids=uuids, fallback_snapshot={}))
                self._claimed_things.add(t.id)
                report.created_in_todoist += 1
                self._event(rule, "created in Todoist", t.title)
                if "title" in values and values["title"] != t.title:
                    # "@name" in the title: it became the assignee, so tidy up the Things side too.
                    tidy = {"title": values["title"]}
                    if values.get("assignee") in person_tag:
                        tidy["assignee"] = values["assignee"]
                    things_ops.append({"op": "update", "id": t.id, "fields": for_things(tidy, t, None)})
                    op_owner.append(None)

        if allow_d2t:
            for d in self._importable(rule, new_d, my_uid):
                values = dfields(d)
                op: Dict[str, Any] = {"op": "create", "fields": for_things(values, None, comments_block(d) or None)}
                place = op["fields"]
                if place.get("project"):
                    op["project"] = place.pop("project")
                elif tscope["type"] == "project":
                    op["project"] = tscope["id"]
                elif tscope["type"] == "area":
                    op["area"] = tscope["id"]
                place.pop("area", None)
                created_in_things.append((len(things_ops), d, values))
                things_ops.append(op)
                op_owner.append(None)
                self._claimed_todoist.add(d.id)
                report.created_in_things += 1
                self._event(rule, "created in Things", d.title)

        for pid, temp, name, uuids in new_groups:
            self._new_groups.append((rule["id"], pid, temp, name, uuids))

        if not things_ops and not project_ops:
            return
        all_ops = [{k: v for k, v in op.items() if k != "_section"} for op in project_ops] + things_ops
        results = self.things.run_ops(all_ops)
        p_results, results = results[:len(project_ops)], results[len(project_ops):]
        for op, res in zip(project_ops, p_results):
            if res.get("ok") and res.get("id"):
                self.state.add_group(rule["id"], res["id"], op["_section"], op["name"])
            else:
                report.errors.append(f"Things project '{op['name']}' could not be created: {res.get('error')}")
        for op, owner, res in zip(things_ops, op_owner, results):
            if not res.get("ok"):
                msg = f"Things {op['op']} failed: {res.get('error')}"
                report.errors.append(msg)
                self._event(rule, "error", op.get("fields", {}).get("title", ""), msg)
                if owner is not None:
                    owner.things_ok = False
        for index, d, values in created_in_things:
            res = results[index] if index < len(results) else {}
            if res.get("ok") and res.get("id"):
                self.state.add_link(rule["id"], res["id"], d.id, values)

    def _people(self, rule: Dict[str, Any], project_id: Optional[str], my_uid: str):
        """(person key -> Todoist user id, user id -> Things tag) for this rule.

        People come from the rule's explicit list and, unless turned off, from the collaborators of
        the Todoist project: a Things tag or "@mention" equal to someone's first name, full name or
        email name (ignoring case, spaces and a leading @ or #) assigns the task to them.
        """
        keys: Dict[str, str] = {}
        tag_of: Dict[str, str] = {}
        for a in rule.get("assignees") or []:
            keys[person_key(a["tag"])] = a["uid"]
            tag_of[a["uid"]] = a["tag"]
        if rule.get("auto_assign", True) and hasattr(self.todoist, "people_in"):
            seen: Dict[str, Optional[str]] = {}
            for p in self.todoist.people_in(project_id):
                if p["id"] == my_uid:
                    continue
                names = [p.get("name", ""), (p.get("name") or "").split(" ")[0], (p.get("email") or "").split("@")[0]]
                for k in {person_key(n) for n in names if n}:
                    seen[k] = None if k in seen and seen[k] != p["id"] else p["id"]  # ambiguous -> skip
                tag_of.setdefault(p["id"], (p.get("name") or p["id"]).split(" ")[0].capitalize())
            for k, uid in seen.items():
                if uid and k not in keys:
                    keys[k] = uid
        if keys and hasattr(self.things, "meta"):
            # Write the tag the user already has in Things (e.g. "Sam") instead of inventing one.
            try:
                existing = self._things_tags()
            except Exception:  # tags are only cosmetic here
                existing = []
            explicit = {a["uid"]: a["tag"] for a in rule.get("assignees") or []}
            for tag in existing:
                uid = keys.get(person_key(tag))
                if uid and explicit.get(uid) not in existing:
                    tag_of[uid] = tag
        return keys, tag_of

    def _things_tags(self) -> List[str]:
        if self._tags_cache is None:
            self._tags_cache = list(self.things.meta().get("tags") or [])
        return self._tags_cache

    def _importable(self, rule: Dict[str, Any], items: List[Item], my_uid: str) -> List[Item]:
        """New Todoist tasks that this rule brings into Things."""
        mode = rule.get("import_mode", "all")
        if mode == "none":
            return []
        if mode == "assigned_to_me":
            return [d for d in items if my_uid and d.extra.get("responsible_uid") == my_uid]
        if mode == "label":
            want = str(rule.get("import_label", "")).strip().casefold()
            return [d for d in items if want in {t.casefold() for t in d.tags}]
        return items
