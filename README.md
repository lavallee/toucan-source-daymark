# toucan-source-daymark

**A Daymark project as a toucan plan source.**

[toucan](https://github.com/lavallee/toucan) is a browser canvas that can show a
plan from a system of record as a board, a gantt chart, a timeline, a dependency
graph or a table, and write back what a person drags. [Daymark](https://github.com/lavallee/daymark)
is a task system on one SQLite file. This small adapter connects the two: toucan
runs it as a command, it reads and writes Daymark, and neither project ships or
depends on the other.

What a person does on the canvas becomes an ordinary Daymark write, with Daymark's
version check, idempotency and history: a card moved to another column sets its
stage (and so its status), a bar dragged on a gantt sets its planned dates, a card
dropped between two others sets its place in the project's order.

## Install

It needs Python 3.11 or newer. **This release, 0.1.0, works with Daymark 0.4.0**:
it installs `daymark-core` pinned to the `v0.4.0` tag, and reads and writes
databases at Daymark 0.4.0's schema (schema 5).

```sh
uv tool install "toucan-source-daymark @ git+https://github.com/lavallee/toucan-source-daymark"
toucan-source-daymark --version
```

`uv tool install` puts the executable in `~/.local/bin` together with its own copy
of `daymark-core`. It opens the same database file as your `daymark` command:
`--db PATH`, else `$DAYMARK_DB`, else Daymark's per-user database
(`$XDG_DATA_HOME/daymark/daymark.sqlite3`). Unlike `daymark`, it does not look for
a `.daymark/` folder above the working directory, because toucan runs it from
somewhere unrelated; set `DAYMARK_DB` for a project database.

It never creates a database and never upgrades one, and it never downgrades one.
A database that a newer Daymark has upgraded answers `unavailable` with a message
naming the Daymark it needs: install the adapter release made for that Daymark.
A database still at an older schema answers `unavailable` too: open it once with
your `daymark` command (0.4.0) to upgrade it, after taking a backup. If the
`daymark-core` it finds is older than 0.4.0, it answers `unavailable` naming the
version it needs.

Upgrade Daymark and the adapter together, so they agree on the schema:

```sh
daymark backup --file before-upgrade.sqlite3      # with the Daymark you are leaving
uv tool install --force "daymark-core @ git+https://github.com/lavallee/daymark@vX.Y.Z"
uv tool install --force "toucan-source-daymark @ git+https://github.com/lavallee/toucan-source-daymark@vA.B.C"
```

where `vA.B.C` is the adapter release whose README names Daymark `X.Y.Z`. Each tool
keeps its own copy of `daymark-core`, so both must move.

## How toucan finds it

toucan looks on `PATH` for executables named `toucan-source-NAME`; this one is the
`daymark` source. It trusts one only from an absolute `PATH` folder outside
toucan's own root and data directory **that is not group- or world-writable**, the
same rule it applies to `toucan-profiles-NAME`. `~/.local/bin` normally qualifies;
check with `ls -ld ~/.local/bin` (no `w` for group or others). Then:

```sh
toucan sources                       # lists daymark and its projects
toucan --canvas C plan bind daymark home/site
toucan --canvas C plan view add board --group-by stage
```

## The protocol

This is the `toucan.plan/1` protocol as this adapter implements it. toucan's
documentation is the canonical source.

```
toucan-source-daymark list                      -> {"sources": [{"ref", "title", "description"?, "readonly"}]}
toucan-source-daymark read REF                  -> a plan
toucan-source-daymark changes REF --after CUR   -> {"cursor": CUR2, "changed": true|false}
toucan-source-daymark write REF   < changes     -> results
```

JSON on standard output and exit 0. On failure: a non-zero exit and one JSON
object on standard error, `{"error": CODE, "message": TEXT}`, where `CODE` is
`not_found` (exit 3), `conflict` (4), `invalid` (5), `forbidden` (7) or
`unavailable` (1).

Options may come anywhere. `--` makes the next argument a reference even if it
looks like an option, and toucan passes it (`read -- demo/rel`,
`changes -- demo/rel --after 184`); a reference that begins with `-` is refused
with `invalid`, so it can never be taken for an option.

A reference is `WORKSPACE/PROJECT_ID`, split at the first `/`. A Daymark workspace
is a hard boundary: a reference reads and writes only its own.

### A plan

```json
{
  "plan": 1,
  "source": {"ref": "home/site", "title": "Website relaunch", "description": "", "readonly": false},
  "cursor": "184",
  "fields": [
    {"id": "stage", "label": "Stage", "type": "option", "writable": true,
     "options": [{"id": "todo", "label": "To do", "color": "grey"},
                 {"id": "done", "label": "Done", "color": "green", "done": true}]},
    {"id": "field:points", "label": "Points", "type": "number", "writable": true}
  ],
  "items": [
    {"id": "dm-ab12", "version": 3, "title": "Draft the sitemap", "detail": null,
     "stage": "todo", "assignee": "ada", "start": "2030-04-10", "end": "2030-04-20",
     "deadline": null, "priority": "2", "labels": [], "dependsOn": [], "parent": null,
     "milestone": false, "inMilestone": "beta", "container": false, "goal": null, "type": "story",
     "done": false, "estimate": 240, "url": null, "status": "open", "field:points": 3}
  ]
}
```

- `items` are the project's tasks in its manual order. `version` changes on every
  write and is what an update must quote. `null` means unset.
- Field types: `text`, `option`, `options`, `date`, `number`, `bool`. An option's
  `color` is one of toucan's colour names; `done: true` marks a finished option.
  `writable: false` means do not offer or attempt to write it.
- `source.readonly: true` means no writes at all.
- `cursor` is the event cursor at the moment of the read; `changes --after` it
  misses nothing.

A proposed task (a suggestion awaiting a person's decision in Daymark) is not an
item: it is not work yet. A cancelled task is an item only if the project has a
cancelled stage.

### Changes

```json
{"cursor": "187", "changed": true}
```

Whether anything the plan shows changed after the cursor: the project, its tasks,
tasks entering or leaving it, what its tasks depend on outside it, its goal, or a
person, label or goal it can name. It is one indexed query, cheap enough to ask
every few seconds: on a synthetic workspace of 20,000 tasks in 101 projects it
takes about 8 ms inside Daymark and about 135 ms as a whole process, most of that
Python start-up, against toucan's 8-second allowance. A cursor from the future (after a restore) answers `true`.

### Write

```json
{"changes": [
  {"op": "update", "id": "dm-ab12", "version": 3, "mutation": "c0ffee",
   "set": {"stage": "done", "end": "2030-12-03"}, "place": {"after": "dm-zz01"}},
  {"op": "create", "mutation": "f00d", "set": {"title": "New card", "stage": "todo"},
   "place": {"after": null}}
]}
```

answers

```json
{"cursor": "189", "results": [
  {"mutation": "c0ffee", "ok": true, "item": {"...": "the item as it now stands"}},
  {"mutation": "f00d", "ok": false, "error": "conflict", "message": "Changed upstream",
   "item": {"...": "the item as it now stands"}}
]}
```

- `set` carries only changed fields, in the plan's ids and value forms; `null`
  clears one. A change that changes nothing (the same values, or a place the item
  already has) writes nothing and answers `ok` with the item as it stands.
- Only items of the plan can be written: a proposed, archived, or cancelled-off-board
  task answers `not_found`. `dependsOn` and `parent` take items of this plan only.
- `place: {"after": ID}` puts the item directly after another; `{"after": null}`
  puts it first. `{"before": ID}` and `{"before": null}` (last) also work. Without
  `place`, a new item goes last.
- `mutation` is an idempotency key, scoped to the project. Sending the same change
  again writes nothing and answers with the item as it stands. Sending a different
  change under a mutation already used answers `conflict`.
- A change's field update and its placement are one transaction. Changes are
  independent of each other: one failing does not stop the rest.
- Each change is one SQLite transaction. If the process is killed mid-write (toucan
  kills it after 30 s), every change is either applied whole or not at all, and
  sending the same write again completes it without duplicates. A write stops
  starting new changes after 20 seconds and reports the rest `unavailable`, to be
  sent again.
- If another process holds Daymark's write lock, the adapter waits up to 5 s
  (`changes`) or 10 s (`read`, each change of a `write`), then answers
  `unavailable`, inside toucan's allowances of 8 s and 30 s. Reading never waits on
  a writer.
- There is no delete. Removing work is a decision made in Daymark (`daymark
  cancel`, `daymark archive`).

## Field mapping

| Plan field | Type | Daymark | Notes |
| --- | --- | --- | --- |
| `title` | text | `title` | |
| `detail` | text | `description` | empty reads as `null` |
| `stage` | option | `stage` | options are the project's stages; writing one sets the task's status to that stage's status |
| `assignee` | option | `assignee_id` | options are the workspace's people |
| `start` | date | `planned`, else `earliest_start` | writing sets `planned` |
| `end` | date | `latest_target`, else `deadline` | writing sets `latest_target`; never moves a deadline |
| `deadline` | date | `deadline` | written only when written as itself; a timed deadline keeps its time |
| `priority` | option | `priority` | `"1"` (highest) to `"4"`; `null` is unset |
| `labels` | options | `label_ids` | options are the workspace's labels |
| `dependsOn` | options (`items: true`) | `depends_on` | only dependencies inside the project; writing keeps the others |
| `parent` | option (`items: true`) | `parent_id` | labelled with the project's container type (for example "Epic") if it has one |
| `milestone` | bool | `milestone` | the marker: the item is itself a milestone (toucan draws a diamond) |
| `inMilestone` | option (`items: true`) | `milestone_id` | labelled "Milestone": the milestone the item counts toward; its options are the project's milestone items |
| `container` | bool, read-only | | true when the item's type is a container type (such as an epic) or it has children; lets a view leave containers out of a lane or column |
| `goal` | option | `goal_id` | options are the workspace's goals |
| `type` | option | `type` | only when the project declares types |
| `done` | bool | `status` | true completes; false reopens a completed or cancelled item |
| `estimate` | number | `estimate_minutes` | minutes |
| `status` | option, read-only | `status` | open, waiting, completed, cancelled |
| `url` | | | always `null` |
| `field:ID` | per type | `values[ID]` | each field the project declares: text, url → text; number; bool; date; option; options; person → option of people. A number field may carry `unit`. A stored value that no longer fits its field (retyped, or its option removed) is sent as `null`, and the field lists those items in `omitted`; Daymark keeps the value. |

`inMilestone`, `container`, `items: true`, `unit`, `omitted`, `source.managedBy` and the list's `readonly` go beyond the
base protocol; a reader may ignore them. Roll-ups (progress, health, milestone and
parent summaries) are not part of the protocol; read them from Daymark itself
(`daymark plan`, `GET /v1/projects/{id}/plan`).

### Dates

Writing `start` or `end` sets Daymark's movable dates (`planned`, `latest_target`)
and never its hard `deadline`. Only the dates a change sets are checked: a start
after an end is refused with `invalid`, never adjusted. A date written back
unchanged leaves the stored value, and its time of day, alone. `start: null`
clears both dates a start reads from (`planned` and `earliest_start`). `end: null`
clears `latest_target`; a deadline still shows through as the end, and is cleared
only by `deadline: null`.

## Read-only projects

A Daymark project with `managed_by` set belongs to another system that rewrites
it. This adapter reads such a project with `readonly: true`, marks every field not
writable, and refuses writes to it with `forbidden`. Mark one with
`daymark project set PROJECT --managed-by SYSTEM`.

## Development

Until Daymark 0.4.0 is tagged, develop against a local Daymark checkout:

```sh
uv venv
uv pip install -e /path/to/daymark          # the local daymark-core
uv pip install -e . --no-deps               # this package, without the git dependency
uv pip install pytest ruff build
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests && uv run --no-sync ruff format --check src tests
```

Once the tag exists, `uv sync --dev` resolves `daymark-core` from it. Tests run the
installed console script as a subprocess against temporary databases; all
records are synthetic.

## License

[MIT](LICENSE).
