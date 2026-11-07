# Things ↔ Todoist Bridge

Two-way sync between **[Things 3](https://culturedcode.com/things/)** and **[Todoist](https://todoist.com)** for the
projects, areas and tags you choose — with a small local dashboard to set it up and watch what it does.

- **Pick what syncs.** A *rule* links one Things project, area or tag to one Todoist project or label.
  Add as many rules as you want.
- **Two-way or one-way**, per rule.
- **Keeps listening.** Runs in the background, polls both apps (Todoist incrementally), and syncs every N seconds.
- **Field-level three-way merge.** Only fields that actually changed are copied; when both sides changed the same
  field, the most recent edit wins. Nothing loops.
- **No duplicates on the first run.** Items with the same title on both sides are linked, not copied.
- **Safe by default.** Deleting on one side only unlinks the other copy unless you opt in to delete propagation.
  A change the other side rejects is retried, never reverted.
- **Zero dependencies.** Pure Python standard library. Nothing leaves your Mac except the calls to Todoist's API.

## What syncs

| Things | Todoist | Notes |
| --- | --- | --- |
| Title | Content | always |
| Notes | Description | optional |
| When (start date) | Due date | optional; recurring Todoist due dates are never overwritten |
| Deadline | Deadline | optional; Todoist deadlines may need a paid plan |
| Tags | Labels | optional; the rule's own tag/label is handled for you |
| Completed / canceled | Completed | always, both ways |

When a rule uses a Things **tag**, new tasks coming from Todoist are created in the Things Inbox with that tag.
When a rule uses a Todoist **label**, new tasks coming from Things are created in the project you pick (Inbox by
default) with that label. Removing the tag/label from an item takes it out of the sync (it is unlinked, not deleted).

## Requirements

- macOS with Things 3
- Python 3.9+ (the one from Homebrew or python.org is fine)
- A Todoist API token: Todoist → Settings → Integrations → Developer

## Install

```bash
git clone https://github.com/<you>/things-todoist-bridge.git
cd things-todoist-bridge
pipx install .            # or: python3 -m venv .venv && .venv/bin/pip install .
```

Or run it straight from the checkout without installing anything: `python3 -m ttbridge`.

## Use

```bash
ttbridge
```

This starts the background sync and opens the dashboard at <http://127.0.0.1:8765>.

1. **Settings** → paste your Todoist API token → *Save* → *Test connections*.
   The first time, macOS asks whether your terminal/Python may control Things — allow it.
2. **Rules** → *Add rule* → pick a Things source and a Todoist target, choose the direction and fields → *Save*.
3. Hit **Sync now** or wait for the next cycle. Watch **Activity** and **Linked items**.

### Run at login

```bash
ttbridge agent install     # starts now and at every login
ttbridge agent status
ttbridge agent uninstall
```

The dashboard stays available at <http://127.0.0.1:8765> while the agent runs.

### Other commands

```bash
ttbridge sync              # run one sync cycle and print a JSON report
ttbridge serve --port 9000 --no-browser
```

## How it works

- **Things** keeps its database in a sandboxed container that macOS won't let other apps read, so the bridge talks to
  Things through JavaScript for Automation (`osascript -l JavaScript`). Bulk reads take ~0.2 s per list.
  Clearing a date isn't possible through scripting, so that one operation uses the Things URL scheme — add your
  Things URL auth token in Settings (Things → Settings → General → Enable Things URLs → Manage) if you want date
  removals to sync from Todoist to Things.
- **Todoist** is accessed with the [API v1 Sync endpoint](https://developer.todoist.com/api/v1/). After the first full
  sync, each poll only fetches what changed. Writes are batched into one request per cycle.
- **State** (links between items and the last-synced snapshot of each pair) lives in SQLite.

For every linked pair the engine compares Things, Todoist and the snapshot field by field:

```
Things changed, Todoist didn't  → copy to Todoist
Todoist changed, Things didn't  → copy to Things
both changed                    → most recent modification wins
```

The snapshot is only updated after the writes it describes succeed.

### Files

Everything lives in `~/Library/Application Support/ThingsTodoistBridge/`:

| File | What |
| --- | --- |
| `config.json` | settings and rules (contains your token, permission `600`) |
| `state.db` | links, snapshots, Todoist cache |
| `ttbridge.log` | log |

Set `TTBRIDGE_HOME` to use a different folder. `TODOIST_API_TOKEN` is used if no token is configured.

### Security

The dashboard only listens on `127.0.0.1`, rejects requests whose `Host` isn't local, and requires a custom header on
every write so other websites open in your browser can't drive it.

## Limitations

- Checklist items inside a Things to-do are not synced (Things doesn't expose them to scripting).
  Todoist sub-tasks sync as normal tasks.
- Things headings, Todoist sections, priorities, reminders and times of day are not synced.
- Changes are picked up by polling (default every 30 s), not instantly.
- Things must be installed on the Mac running the bridge; it is launched in the background when needed
  (can be turned off in Settings).

## Development

```bash
python3 -m unittest discover -s tests -v
```

The tests use in-memory fakes of Things and of the Todoist Sync API, so they run anywhere.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Cultured Code or Doist.
