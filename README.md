# things-todoist-bridge

I plan my work in [Things 3](https://culturedcode.com/things/). The people I work with use
[Todoist](https://todoist.com). This keeps the two in step, for only the parts of Things I pick, so I never have to
copy a task by hand or ask "did you see my comment?".

It runs on your Mac, checks both apps every 30 seconds and comes with a small dashboard in the browser where you set
it up and see what it did.

## What it does

You write **rules**. A rule connects one thing in Things to one place in Todoist:

| Things | Todoist |
| --- | --- |
| a project | a project |
| an area (with all the projects in it) | a project, with one section per Things project |
| a tag, e.g. `todoist` | a project, or a label |

Nothing outside your rules is read or touched. Tag a to-do with `todoist` and it shows up in Todoist; take the tag
off and it stops syncing (it isn't deleted).

For each linked pair these fields are kept the same:

| Things | Todoist |
| --- | --- |
| title | task name |
| notes | description |
| when | due date (recurring dates in Todoist are left alone) |
| deadline | deadline |
| tags | labels |
| completed / canceled | completed |
| project inside an area | section |
| a tag with a teammate's name | assignee |
| — | comments, shown at the bottom of the Things notes |

If only one side changed a field, that change wins. If both changed it since the last sync, the newer one wins.

### Working with someone who uses Todoist

This is the part I built it for.

**Sending work.** Put the to-do in a synced project (or give it the rule's tag). It appears in the shared Todoist
project.

**Assigning.** Tag the to-do with your teammate's name, e.g. `Sam`, `@sam` or `#Sam`, or write `@sam` in the title.
It gets assigned to Sam in Todoist and the `@sam` is removed from the title. If Sam hands it back to you, the tag
goes away. Anyone the Todoist project is shared with can be assigned this way; the rule editor lists them. For tags
that aren't a person's name (`design-team` → Sam) you can add your own pairs.

**Their updates.** When Sam edits the description, moves the date or completes the task, Things follows. Their
comments are added under your notes, below a `── Todoist comments ──` line. Your own notes stay as they are and keep
syncing with the description; the comments block is never sent back.

**Their new tasks.** Each rule decides which new Todoist tasks come into Things: all of them, only the ones assigned
to you, only the ones with a label you choose, or none.

### Areas

A rule on an area takes the to-dos in the area and in all its open projects. The projects themselves are not tasks:
each one becomes a section in the Todoist project (an existing section with the same name is reused). Move a to-do
to another project and it moves to that section in Todoist, and the other way round. If someone adds a new section in
Todoist, a project with that name is created in the area as soon as one of its tasks comes over.

Prefer no sections? Pick *No sections* in the rule.

### Deleting

By default deleting a task on one side only unlinks the copy on the other side, and that copy is left alone from then
on. If you'd rather delete both, set *When an item is deleted on one side* to *Delete the other copy too*. Things
moves deleted to-dos to its Trash.

## Setup

You need macOS with Things 3, Python 3.9 or newer, and a Todoist API token (Todoist → Settings → Integrations →
Developer).

```bash
git clone https://github.com/burakodabas/things-todoist-bridge.git
cd things-todoist-bridge
pipx install .
ttbridge
```

No pipx? `python3 -m venv .venv && .venv/bin/pip install .`, or run it straight from the folder with
`python3 -m ttbridge`.

The dashboard opens at <http://127.0.0.1:8765>.

1. **Settings**: paste the Todoist token, save, then *Test connections*. macOS asks once whether Terminal (or Python)
   may control Things. Say yes.
2. **Rules** → *Add rule*: pick the Things side, the Todoist side and the options.
3. Hit *Sync now*, or wait for the next round. *Activity* shows every change it made.

Optional: Things can't clear a date through scripting, so removing a date in Todoist only reaches Things if you add
your Things URL token in Settings (Things → Settings → General → Enable Things URLs → Manage).

### Keep it running

```bash
ttbridge agent install     # start now and at every login
ttbridge agent status
ttbridge agent uninstall
```

Other commands:

```bash
ttbridge sync              # one round, prints what changed
ttbridge serve --port 9000 --no-browser
```

## How it works

Things keeps its database in a folder other apps aren't allowed to read, so the bridge talks to Things through
JavaScript for Automation (`osascript -l JavaScript`). Reading a list of to-dos takes a fraction of a second.

Todoist is read through the [Sync API](https://developer.todoist.com/api/v1/). After the first run each round only
downloads what changed, and all writes go out in one request.

For every linked pair the bridge stores the values from the last sync in a small SQLite file. That snapshot is how it
can tell which side changed, and it is only updated once the write to the other side actually succeeded, so a failed
write is retried instead of being undone.

Everything lives in `~/Library/Application Support/ThingsTodoistBridge/`:

| File | |
| --- | --- |
| `config.json` | settings and rules, including your token (readable only by you) |
| `state.db` | links between tasks and the Todoist cache |
| `ttbridge.log` | log |

`TTBRIDGE_HOME` moves that folder; `TODOIST_API_TOKEN` is used when no token is saved.

The dashboard only listens on `127.0.0.1`, refuses requests that don't come from `localhost`, and needs a custom
header on every change, so a web page you have open can't drive it. There are no dependencies outside the Python
standard library, and nothing is sent anywhere except to Todoist.

## Known gaps

- Checklists inside a Things to-do, headings, priorities, reminders and times of day don't sync.
  Todoist sub-tasks come over as normal to-dos.
- Comments only go from Todoist to Things.
- Changes are noticed by polling, so they take up to the interval you set (30 seconds by default).
- Things has to be installed on the Mac that runs the bridge. It is started in the background when needed; you can
  turn that off in Settings.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests run against in-memory fakes of Things and the Todoist Sync API, so they don't need either app.

## License

MIT. Not affiliated with Cultured Code or Doist.
