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
import time
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
    return tuple(fields)


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

        t_items = self.things.scope_items(tscope)
        d_items = self.todoist.scope_items(dscope)
        links = self.state.links(rule["id"])

        missing = [l["things_id"] for l in links if l["things_id"] not in t_items]
        t_extra = self.things.get_items(missing) if missing else {}

        things_ops: List[Dict[str, Any]] = []
        op_owner: List[Optional[PendingSnapshot]] = []
        created_in_things: List[Tuple[int, Item, Dict[str, Any]]] = []

        def tfields(t: Item) -> Dict[str, Any]:
            return {k: v for k, v in t.fields(t_scope_tag).items() if k in fields}

        def dfields(d: Item) -> Dict[str, Any]:
            return {k: v for k, v in d.fields(d_scope_label).items() if k in fields}

        def for_things(values: Dict[str, Any], force_tags: bool = False) -> Dict[str, Any]:
            p = dict(values)
            if "tags" in p or force_tags:
                p["tags"] = list(norm_tags(list(p.get("tags", [])) + list(t_scope_tag)))
            return p

        def for_todoist(values: Dict[str, Any], force_tags: bool = False) -> Dict[str, Any]:
            p = dict(values)
            if "tags" in p or force_tags:
                p["tags"] = list(norm_tags(list(p.get("tags", [])) + list(d_scope_label)))
            return p

        def apply_merge(t: Item, d: Item, snap: Dict[str, Any], pending: PendingSnapshot) -> None:
            tf, df = tfields(t), dfields(d)
            result = merge(fields, tf, df, snap, t.modified, d.modified, direction)
            if d.recurring and "when" in result and not _eq("when", result["when"], df.get("when")):
                result["when"] = df.get("when")  # recurring due dates are owned by Todoist
            t_patch = {f: result[f] for f in fields if allow_d2t and not _eq(f, result[f], tf.get(f))}
            d_patch = {f: result[f] for f in fields if allow_t2d and not _eq(f, result[f], df.get(f))}
            title = result.get("title") or t.title
            if t_patch:
                things_ops.append({"op": "update", "id": t.id, "fields": for_things(t_patch)})
                op_owner.append(pending)
                report.updated_things += 1
                self._event(rule, "updated in Things", title, ", ".join(t_patch))
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
            t_gone = t is None or t.deleted
            t_out = (not t_gone) and tid not in t_items and not t.completed

            d = self.todoist.get_item(did)
            if d is None:
                # Not in the cache (e.g. completed while the cache was rebuilt): treat as completed.
                d = Item(id=did, title=snap.get("title", ""), notes=snap.get("notes", ""),
                         when=snap.get("when"), deadline=snap.get("deadline"),
                         tags=snap.get("tags", []), completed=True, modified=0.0)
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
                self.state.remove_link(tid)
                continue

            if t_out or d_out:
                self.state.remove_link(tid)
                report.unlinked += 1
                self._event(rule, "unlinked", title, "moved out of scope in " + ("Things" if t_out else "Todoist"))
                continue

            apply_merge(t, d, snap, PendingSnapshot(rule["id"], tid, did, snap))

        # ---- unlinked items ---------------------------------------------
        linked_t = self.state.linked_things_ids() | self._claimed_things
        linked_d = self.state.linked_todoist_ids() | self._claimed_todoist
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
            if dscope["type"] == "project":
                target_project = dscope["id"]
            else:
                target_project = dscope.get("create_project_id") or self.todoist.inbox_id()
            for t in unmatched_t:
                values = tfields(t)
                temp_id, uuids = self._todoist(
                    rule, t.title, lambda: self.todoist.add(target_project, for_todoist(values, force_tags=True)))
                self._pending.append(PendingSnapshot(rule["id"], t.id, temp_id, values, new_link=True,
                                                     temp_id=True, todoist_uuids=uuids, fallback_snapshot={}))
                self._claimed_things.add(t.id)
                report.created_in_todoist += 1
                self._event(rule, "created in Todoist", t.title)

        if allow_d2t:
            for d in new_d:
                values = dfields(d)
                op: Dict[str, Any] = {"op": "create", "fields": for_things(values, force_tags=True)}
                if tscope["type"] == "project":
                    op["project"] = tscope["id"]
                elif tscope["type"] == "area":
                    op["area"] = tscope["id"]
                created_in_things.append((len(things_ops), d, values))
                things_ops.append(op)
                op_owner.append(None)
                self._claimed_todoist.add(d.id)
                report.created_in_things += 1
                self._event(rule, "created in Things", d.title)

        if not things_ops:
            return
        results = self.things.run_ops(things_ops)
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
