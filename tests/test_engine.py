import tempfile
import unittest
from pathlib import Path

from ttbridge.config import normalise_rule
from ttbridge.engine import SyncEngine, merge
from ttbridge.state import State
from ttbridge.todoist import TodoistClient

from fakes import FakeThings, FakeTodoistServer


class Base(unittest.TestCase):
    rule_spec = {"things": {"type": "project", "id": "TP1", "name": "Home"},
                 "todoist": {"type": "project", "id": "P_work", "name": "Work"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.tmp.name) / "state.db")
        self.things = FakeThings()
        self.server = FakeTodoistServer()
        self.todoist = TodoistClient("tok123456", self.state, opener=self.server)
        self.events = []
        self.engine = SyncEngine(self.things, self.todoist, self.state, self.events.append)
        self.rule = normalise_rule(dict(self.rule_spec, **getattr(self, "rule_extra", {})))

    def tearDown(self):
        self.state.db.close()
        self.tmp.cleanup()

    def sync(self, rule=None):
        return self.engine.run([rule or self.rule])[0]

    def todo(self, id_):
        return self.server.items[id_]

    def only_link(self):
        links = self.state.links()
        self.assertEqual(len(links), 1)
        return links[0]


class TestMerge(unittest.TestCase):
    F = ("title", "notes", "completed")

    def test_one_side_changed(self):
        snap = {"title": "a", "notes": "", "completed": False}
        r = merge(self.F, {"title": "b", "notes": "", "completed": False}, dict(snap), snap, 1, 2, "both")
        self.assertEqual(r["title"], "b")

    def test_both_changed_newer_wins(self):
        snap = {"title": "a", "notes": "", "completed": False}
        t = {"title": "things", "notes": "", "completed": False}
        d = {"title": "todoist", "notes": "", "completed": False}
        self.assertEqual(merge(self.F, t, d, snap, 5, 3, "both")["title"], "things")
        self.assertEqual(merge(self.F, t, d, snap, 3, 5, "both")["title"], "todoist")

    def test_one_way(self):
        snap = {"title": "a", "notes": "", "completed": False}
        t = {"title": "a", "notes": "", "completed": False}
        d = {"title": "changed", "notes": "", "completed": False}
        self.assertEqual(merge(self.F, t, d, snap, 1, 2, "things_to_todoist")["title"], "a")


class TestProjectRule(Base):
    def test_things_to_todoist_create_and_no_loop(self):
        tid = self.things.add("Buy milk", project="TP1", notes="2L", tags=["Home"], deadline="2026-11-20")
        rep = self.sync()
        self.assertEqual(rep["created_in_todoist"], 1)
        link = self.only_link()
        task = self.todo(link["todoist_id"])
        self.assertEqual(task["content"], "Buy milk")
        self.assertEqual(task["description"], "2L")
        self.assertEqual(task["labels"], ["Home"])
        self.assertEqual(task["deadline"], {"date": "2026-11-20"})
        self.assertEqual(link["things_id"], tid)
        # Second and third runs change nothing.
        for _ in range(2):
            rep = self.sync()
            self.assertEqual((rep["updated_todoist"], rep["updated_things"], rep["created_in_things"]), (0, 0, 0))

    def test_todoist_to_things_create(self):
        did = self.server.add("Call mom", due={"date": "2026-11-10", "is_recurring": False})
        rep = self.sync()
        self.assertEqual(rep["created_in_things"], 1)
        link = self.only_link()
        self.assertEqual(link["todoist_id"], did)
        t = self.things.items[link["things_id"]]
        self.assertEqual((t["title"], t["when"], t["project"]), ("Call mom", "2026-11-10", "TP1"))
        rep = self.sync()
        self.assertEqual(rep["updated_things"] + rep["updated_todoist"] + rep["created_in_todoist"], 0)

    def test_edit_propagates_both_ways(self):
        tid = self.things.add("Draft", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.things.edit(tid, title="Draft v2")
        self.sync()
        self.assertEqual(self.todo(did)["content"], "Draft v2")
        self.server.edit(did, description="from todoist")
        self.sync()
        self.assertEqual(self.things.items[tid]["notes"], "from todoist")

    def test_completion_propagates(self):
        tid = self.things.add("Pay rent", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.things.edit(tid, completed=True)
        self.sync()
        self.assertTrue(self.todo(did)["checked"])
        # Reopen from Todoist
        self.server.edit(did, checked=False)
        self.sync()
        self.assertFalse(self.things.items[tid]["completed"])

    def test_todoist_completion_reaches_things(self):
        did = self.server.add("Water plants")
        self.sync()
        tid = self.only_link()["things_id"]
        self.server.edit(did, checked=True)
        self.sync()
        self.assertTrue(self.things.items[tid]["completed"])

    def test_title_match_on_first_run(self):
        self.things.add("Same task", project="TP1")
        self.server.add("same task")
        rep = self.sync()
        self.assertEqual((rep["matched"], rep["created_in_things"], rep["created_in_todoist"]), (1, 0, 0))
        self.assertEqual(len(self.server.items), 1)
        self.assertEqual(len(self.things.items), 1)

    def test_delete_unlinks_by_default(self):
        tid = self.things.add("Temp", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.things.trash.add(tid)
        rep = self.sync()
        self.assertEqual(rep["unlinked"], 1)
        self.assertFalse(self.todo(did)["is_deleted"])
        self.assertEqual(self.state.links(), [])

    def test_delete_propagates_when_enabled(self):
        rule = normalise_rule(dict(self.rule_spec, on_delete="delete"))
        tid = self.things.add("Temp", project="TP1")
        self.sync(rule)
        did = self.only_link()["todoist_id"]
        self.things.trash.add(tid)
        self.sync(rule)
        self.assertTrue(self.todo(did)["is_deleted"])
        did2 = self.server.add("Other")
        self.sync(rule)
        tid2 = [l for l in self.state.links() if l["todoist_id"] == did2][0]["things_id"]
        self.server.edit(did2, is_deleted=True)
        self.sync(rule)
        self.assertIn(tid2, self.things.trash)

    def test_moved_out_of_scope_unlinks(self):
        tid = self.things.add("Move me", project="TP1")
        self.sync()
        self.things.edit(tid, project="OTHER")
        rep = self.sync()
        self.assertEqual(rep["unlinked"], 1)
        self.assertEqual(rep["created_in_things"], 0)

    def test_rejected_todoist_write_is_retried_not_reverted(self):
        tid = self.things.add("Report", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.server.reject.add("item_update")
        self.things.edit(tid, deadline="2026-12-01")
        rep = self.sync()
        self.assertTrue(rep["errors"])
        # Things must keep its new value even though Todoist rejected it.
        self.sync()
        self.assertEqual(self.things.items[tid]["deadline"], "2026-12-01")
        self.server.reject.clear()
        self.sync()
        self.assertEqual(self.todo(did)["deadline"], {"date": "2026-12-01"})

    def test_failed_things_write_is_retried(self):
        did = self.server.add("Thing")
        self.sync()
        tid = self.only_link()["things_id"]
        self.server.edit(did, content="Thing renamed")
        self.things.fail_next_update = True
        self.sync()
        self.assertEqual(self.things.items[tid]["title"], "Thing")
        self.sync()
        self.assertEqual(self.things.items[tid]["title"], "Thing renamed")
        self.assertEqual(self.todo(did)["content"], "Thing renamed")

    def test_recurring_due_not_overwritten(self):
        did = self.server.add("Weekly review", due={"date": "2026-11-09", "is_recurring": True})
        self.sync()
        tid = self.only_link()["things_id"]
        self.things.edit(tid, when="2026-11-12")
        self.sync()
        self.assertEqual(self.todo(did)["due"]["date"], "2026-11-09")
        self.assertEqual(self.things.items[tid]["when"], "2026-11-09")

    def test_one_way_things_to_todoist(self):
        rule = normalise_rule(dict(self.rule_spec, direction="things_to_todoist"))
        self.server.add("Todoist only")
        tid = self.things.add("Things only", project="TP1")
        rep = self.sync(rule)
        self.assertEqual((rep["created_in_todoist"], rep["created_in_things"]), (1, 0))
        did = self.only_link()["todoist_id"]
        self.server.edit(did, content="edited in todoist")
        self.sync(rule)
        self.assertEqual(self.todo(did)["content"], "Things only")
        self.assertEqual(self.things.items[tid]["title"], "Things only")

    def test_restart_keeps_incremental_cache(self):
        self.things.add("Persist", project="TP1")
        self.sync()
        client2 = TodoistClient("tok123456", self.state, opener=self.server)
        self.assertNotEqual(client2.sync_token, "*")
        engine2 = SyncEngine(self.things, client2, self.state)
        rep = engine2.run([self.rule])[0]
        self.assertEqual(rep["created_in_todoist"] + rep["created_in_things"], 0)


class TestTagLabelRule(Base):
    rule_spec = {"things": {"type": "tag", "id": "", "name": "WORK"},
                 "todoist": {"type": "label", "id": "", "name": "work", "create_project_id": ""}}

    def test_scope_tag_and_label_handled(self):
        tid = self.things.add("Ship it", tags=["WORK", "Important"])
        self.sync()
        did = self.only_link()["todoist_id"]
        task = self.todo(did)
        self.assertEqual(task["project_id"], "P_inbox")
        self.assertEqual(sorted(task["labels"]), ["Important", "work"])
        # Todoist side adds a label; Things keeps its scope tag.
        self.server.edit(did, labels=["work", "Important", "Urgent"])
        self.sync()
        self.assertEqual(sorted(self.things.items[tid]["tags"]), ["Important", "Urgent", "WORK"])
        # Removing the scope tag in Things unlinks instead of syncing the removal.
        self.things.edit(tid, tags=["Important", "Urgent"])
        rep = self.sync()
        self.assertEqual(rep["unlinked"], 1)

    def test_todoist_label_creates_tagged_things_item(self):
        self.server.add("From todoist", labels=["work"])
        self.sync()
        tid = self.only_link()["things_id"]
        self.assertEqual(self.things.items[tid]["tags"], ["WORK"])

    def test_two_rules_do_not_duplicate(self):
        other = normalise_rule({"things": {"type": "project", "id": "TP1", "name": "Home"},
                                "todoist": {"type": "label", "id": "", "name": "work"}})
        self.things.add("In both", project="TP1", tags=["WORK"])
        self.engine.run([self.rule, other])
        self.engine.run([self.rule, other])
        self.assertEqual(len(self.server.items), 1)
        self.assertEqual(len(self.things.items), 1)


class TestAreaRule(Base):
    rule_spec = {"things": {"type": "area", "id": "A1", "name": "Work area"},
                 "todoist": {"type": "project", "id": "P_work", "name": "Work"}}

    def test_projects_become_sections_not_tasks(self):
        pa = self.things.add_project("Launch", area="A1")
        self.things.add("Loose task", area="A1")
        self.things.add("Write copy", project=pa)
        rep = self.sync()
        self.assertEqual(rep["created_in_todoist"], 2)
        titles = {i["content"]: i for i in self.server.items.values()}
        self.assertNotIn("Launch", titles)
        self.assertEqual(len(self.server.sections), 1)
        sid = next(iter(self.server.sections))
        self.assertEqual(self.server.sections[sid]["name"], "Launch")
        self.assertEqual(titles["Write copy"]["section_id"], sid)
        self.assertIsNone(titles["Loose task"]["section_id"])
        rep = self.sync()
        self.assertEqual(rep["updated_todoist"] + rep["updated_things"] + rep["created_in_things"], 0)

    def test_existing_section_matched_by_name(self):
        sid = self.server.add_section("launch")
        pa = self.things.add_project("Launch", area="A1")
        self.things.add("Task", project=pa)
        self.sync()
        self.assertEqual(len(self.server.sections), 1)
        self.assertEqual(self.todo(self.only_link()["todoist_id"])["section_id"], sid)

    def test_moves_follow_both_ways(self):
        pa = self.things.add_project("Alpha", area="A1")
        pb = self.things.add_project("Beta", area="A1")
        tid = self.things.add("Move me", project=pa)
        self.sync()
        did = self.only_link()["todoist_id"]
        sec = {s["name"]: i for i, s in self.server.sections.items()}
        self.things.edit(tid, project=pb)
        self.sync()
        self.assertEqual(self.todo(did)["section_id"], sec["Beta"])
        self.server.edit(did, section_id=None)
        self.sync()
        self.assertIsNone(self.things.items[tid]["project"])
        self.assertEqual(self.things.items[tid]["area"], "A1")
        self.server.edit(did, section_id=sec["Alpha"])
        self.sync()
        self.assertEqual(self.things.items[tid]["project"], pa)

    def test_new_todoist_section_creates_things_project(self):
        sid = self.server.add_section("Teammate stuff")
        self.server.add("Their task", section_id=sid)
        rep = self.sync()
        self.assertEqual(rep["created_in_things"], 1)
        tid = self.only_link()["things_id"]
        proj = self.things.items[tid]["project"]
        self.assertEqual(self.things.projects[proj]["name"], "Teammate stuff")
        self.assertEqual(self.things.projects[proj]["area"], "A1")
        rep = self.sync()
        self.assertEqual(rep["updated_todoist"] + rep["updated_things"] + rep["created_in_things"], 0)
        self.assertEqual(len(self.things.projects), 1)

    def test_old_project_links_are_dropped(self):
        pa = self.things.add_project("Launch", area="A1")
        did = self.server.add("Launch")
        self.state.add_link(self.rule["id"], pa, did, {"title": "Launch"})
        rep = self.sync()
        self.assertEqual(rep["unlinked"], 1)
        self.assertNotIn(pa, self.state.linked_things_ids())

    def test_flat_mode(self):
        rule = normalise_rule(dict(self.rule_spec, area_projects="flat"))
        pa = self.things.add_project("Launch", area="A1")
        self.things.add("Write copy", project=pa)
        self.sync(rule)
        self.assertEqual(self.server.sections, {})
        self.assertEqual([i["content"] for i in self.server.items.values()], ["Write copy"])


class TestImportModes(Base):
    def test_none_imports_nothing_but_edits_flow_back(self):
        rule = normalise_rule(dict(self.rule_spec, import_mode="none"))
        self.server.add("Teammate task")
        tid = self.things.add("Mine", project="TP1")
        rep = self.sync(rule)
        self.assertEqual((rep["created_in_things"], rep["created_in_todoist"]), (0, 1))
        did = self.only_link()["todoist_id"]
        self.server.edit(did, description="update from teammate")
        self.sync(rule)
        self.assertEqual(self.things.items[tid]["notes"], "update from teammate")

    def test_assigned_to_me(self):
        rule = normalise_rule(dict(self.rule_spec, import_mode="assigned_to_me"))
        self.server.add("For me", responsible_uid="U_me")
        self.server.add("For Mert", responsible_uid="U_mert")
        self.server.add("Nobody")
        self.sync(rule)
        self.assertEqual([t["title"] for t in self.things.items.values()], ["For me"])

    def test_label(self):
        rule = normalise_rule(dict(self.rule_spec, import_mode="label", import_label="things"))
        self.server.add("Pull me", labels=["things"])
        self.server.add("Leave me")
        self.sync(rule)
        self.assertEqual([t["title"] for t in self.things.items.values()], ["Pull me"])

    def test_label_mode_needs_label(self):
        with self.assertRaises(ValueError):
            normalise_rule(dict(self.rule_spec, import_mode="label"))


class TestComments(Base):
    def test_comments_appended_to_things_notes(self):
        tid = self.things.add("Review", project="TP1", notes="my notes")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.server.comment(did, "Looks good, one fix needed")
        self.sync()
        notes = self.things.items[tid]["notes"]
        self.assertTrue(notes.startswith("my notes\n\n── Todoist comments ──"))
        self.assertIn("Mert: Looks good, one fix needed", notes)
        # The comments block never goes back to Todoist, and nothing loops.
        self.assertEqual(self.todo(did)["description"], "my notes")
        rep = self.sync()
        self.assertEqual(rep["updated_things"] + rep["updated_todoist"], 0)
        # Editing my own notes keeps the block.
        self.things.edit(tid, notes=notes.replace("my notes", "my new notes"))
        self.sync()
        self.assertEqual(self.todo(did)["description"], "my new notes")
        self.assertIn("Mert: Looks good", self.things.items[tid]["notes"])

    def test_comments_off(self):
        rule = normalise_rule(dict(self.rule_spec, sync_comments=False))
        tid = self.things.add("Review", project="TP1")
        self.sync(rule)
        self.server.comment(self.only_link()["todoist_id"], "hi")
        self.sync(rule)
        self.assertEqual(self.things.items[tid]["notes"], "")


class TestAssignees(Base):
    rule_extra = {"assignees": [{"tag": "@Mert", "uid": "U_mert", "name": "Mert"}]}

    def test_tag_assigns_in_todoist_and_back(self):
        tid = self.things.add("Design logo", project="TP1", tags=["@Mert", "Design"])
        self.sync()
        did = self.only_link()["todoist_id"]
        self.assertEqual(self.todo(did)["responsible_uid"], "U_mert")
        self.assertEqual(self.todo(did)["labels"], ["Design"])
        # Mert hands it back to me in Todoist -> tag removed in Things.
        self.server.edit(did, responsible_uid="U_me")
        self.sync()
        self.assertEqual(self.things.items[tid]["tags"], ["Design"])
        # Assign again from Things.
        self.things.edit(tid, tags=["Design", "@Mert"])
        self.sync()
        self.assertEqual(self.todo(did)["responsible_uid"], "U_mert")
        rep = self.sync()
        self.assertEqual(rep["updated_things"] + rep["updated_todoist"], 0)

    def test_unknown_assignee_is_kept(self):
        did = self.server.add("Theirs", responsible_uid="U_other")
        self.sync()
        tid = self.only_link()["things_id"]
        self.things.edit(tid, title="Theirs v2")
        self.sync()
        self.assertEqual(self.todo(did)["responsible_uid"], "U_other")
        self.assertEqual(self.todo(did)["content"], "Theirs v2")

    def test_new_todoist_task_assigned_to_mert_gets_tag(self):
        self.server.add("Mert's", responsible_uid="U_mert")
        self.sync()
        tid = self.only_link()["things_id"]
        self.assertEqual(self.things.items[tid]["tags"], ["@Mert"])


class TestAutoAssign(Base):
    def test_tag_with_collaborator_name_assigns(self):
        for tag in ("Mert", "@mert", "#MERT"):
            with self.subTest(tag=tag):
                self.setUp()
                tid = self.things.add("Ship " + tag, project="TP1", tags=[tag, "Design"])
                self.sync()
                task = self.todo(self.only_link()["todoist_id"])
                self.assertEqual(task["responsible_uid"], "U_mert")
                self.assertEqual(task["labels"], ["Design"])
                self.assertEqual(self.sync()["updated_todoist"] + self.sync()["updated_things"], 0)
                self.assertEqual(sorted(self.things.items[tid]["tags"]), sorted([tag, "Design"]))

    def test_mention_in_title(self):
        tid = self.things.add("Write the post @mert", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.assertEqual(self.todo(did)["content"], "Write the post")
        self.assertEqual(self.todo(did)["responsible_uid"], "U_mert")
        self.assertEqual(self.things.items[tid]["title"], "Write the post")
        self.assertEqual(self.things.items[tid]["tags"], ["Mert"])
        rep = self.sync()
        self.assertEqual(rep["updated_todoist"] + rep["updated_things"], 0)

    def test_existing_things_tag_is_reused(self):
        self.things.extra_tags = ["mert"]
        self.server.add("From Mert", responsible_uid="U_mert")
        self.sync()
        tid = self.only_link()["things_id"]
        self.assertEqual(self.things.items[tid]["tags"], ["mert"])

    def test_mention_already_in_todoist_title_is_cleaned(self):
        tid = self.things.add("Report", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.server.edit(did, content="Report @Mert")
        self.sync()
        self.assertEqual(self.todo(did)["content"], "Report")
        self.assertEqual(self.todo(did)["responsible_uid"], "U_mert")
        self.assertEqual(self.things.items[tid]["title"], "Report")
        self.assertEqual(self.things.items[tid]["tags"], ["Mert"])

    def test_unknown_mention_left_alone(self):
        self.things.add("Ping @nobody", project="TP1")
        self.sync()
        task = self.todo(self.only_link()["todoist_id"])
        self.assertEqual((task["content"], task["responsible_uid"]), ("Ping @nobody", "U_me"))

    def test_off(self):
        rule = normalise_rule(dict(self.rule_spec, auto_assign=False))
        self.things.add("Ship", project="TP1", tags=["Mert"])
        self.sync(rule)
        task = self.todo(self.only_link()["todoist_id"])
        self.assertEqual((task["responsible_uid"], task["labels"]), (None, ["Mert"]))


class TestDeletedStaysDeleted(Base):
    def test_deleted_in_todoist_not_recreated(self):
        tid = self.things.add("Junk", project="TP1")
        self.sync()
        did = self.only_link()["todoist_id"]
        self.server.edit(did, is_deleted=True)
        self.sync()
        rep = self.sync()
        self.assertEqual(rep["created_in_todoist"], 0)
        self.assertIn(tid, self.things.items)
        self.assertEqual(len([i for i in self.server.items.values() if not i["is_deleted"]]), 0)

    def test_deleted_in_things_not_reimported(self):
        tid = self.things.add("Junk", project="TP1")
        self.sync()
        self.things.trash.add(tid)
        self.sync()
        rep = self.sync()
        self.assertEqual(rep["created_in_things"], 0)


class TestAssignMeByDefault(Base):
    def test_new_task_from_things_is_mine(self):
        self.things.add("Mine", project="TP1")
        self.sync()
        self.assertEqual(self.todo(self.only_link()["todoist_id"])["responsible_uid"], "U_me")

    def test_removing_person_tag_hands_back_to_me(self):
        tid = self.things.add("Pass it", project="TP1", tags=["Mert"])
        self.sync()
        did = self.only_link()["todoist_id"]
        self.things.edit(tid, tags=[])
        self.sync()
        self.assertEqual(self.todo(did)["responsible_uid"], "U_me")

    def test_teammates_unassigned_task_left_alone(self):
        did = self.server.add("Team task")
        self.sync()
        self.sync()
        self.assertIsNone(self.todo(did)["responsible_uid"])

    def test_off(self):
        rule = normalise_rule(dict(self.rule_spec, assign_me_by_default=False))
        self.things.add("Nobody's", project="TP1")
        self.sync(rule)
        self.assertIsNone(self.todo(self.only_link()["todoist_id"])["responsible_uid"])

    def test_not_in_unshared_project(self):
        self.server.collaborator_states = [s for s in self.server.collaborator_states if s["user_id"] == "U_me"]
        self.things.add("Solo", project="TP1")
        self.sync()
        self.assertIsNone(self.todo(self.only_link()["todoist_id"])["responsible_uid"])


if __name__ == "__main__":
    unittest.main()
