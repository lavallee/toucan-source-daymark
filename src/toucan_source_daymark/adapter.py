"""toucan-source-daymark: a Daymark project as a toucan plan source.

toucan draws a plan (a board, a gantt, a timeline, a dependency graph, a table)
from a system of record and writes back what a person changes on the canvas.
This adapter is that seam for Daymark. It speaks the ``toucan.plan/1`` protocol
on standard input and output:

    toucan-source-daymark list                       projects in every workspace
    toucan-source-daymark read WORKSPACE/PROJECT     the plan
    toucan-source-daymark changes REF --after CUR    {"cursor", "changed"}
    toucan-source-daymark write REF < changes.json   per-change results

It opens ``--db PATH``, else ``$DAYMARK_DB``, else Daymark's per-user database,
and never creates or upgrades a database. A failure exits non-zero with one JSON
object on standard error: ``{"error": CODE, "message": TEXT}``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

from daymark import DomainError, Store, plans
from daymark.tasks import local_timezone

CODES = {"not_found": 3, "conflict": 4, "invalid": 5, "forbidden": 7, "unavailable": 1}
_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"
PRIORITIES = [
    {"id": "1", "label": "P1", "color": "red"},
    {"id": "2", "label": "P2", "color": "orange"},
    {"id": "3", "label": "P3", "color": "yellow"},
    {"id": "4", "label": "P4", "color": "grey"},
]
STATUSES = [
    {"id": "open", "label": "Open"},
    {"id": "waiting", "label": "Waiting"},
    {"id": "completed", "label": "Completed", "done": True},
    {"id": "cancelled", "label": "Cancelled"},
]
# Daymark custom field types as plan field types.
FIELD_TYPES = {
    "text": "text",
    "url": "text",
    "number": "number",
    "bool": "bool",
    "date": "date",
    "option": "option",
    "options": "options",
    "person": "option",
}
CUSTOM = "field:"
WRITABLE = ("title", "detail", "stage", "assignee", "start", "end", "deadline", "priority",
            "labels", "dependsOn", "parent", "milestone", "inMilestone", "goal", "type", "done",
            "estimate")  # fmt: skip


class Failure(Exception):
    def __init__(self, code, message, item_id=None):
        self.code = code
        self.item_id = item_id
        super().__init__(message)


def database(explicit=None):
    """--db, then $DAYMARK_DB, then the per-user database (as `daymark init` makes it)."""
    if explicit:
        return explicit
    if os.environ.get("DAYMARK_DB"):
        return os.environ["DAYMARK_DB"]
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return str(home / "daymark" / "daymark.sqlite3")


def _open(path):
    if path != ":memory:" and not Path(path).is_file():
        raise Failure("unavailable", "No Daymark database; run `daymark init`")
    try:
        return Store(path, upgrade=False)  # Never upgrade a file someone else owns.
    except DomainError as exc:
        raise Failure("unavailable", str(exc)) from None


def _ref(ref):
    workspace, _, project = (ref or "").partition("/")
    if not workspace or not project or ref.startswith("-"):
        raise Failure("invalid", "A reference is WORKSPACE/PROJECT_ID")
    return workspace, project


def _project(store, workspace, project_id):
    try:
        project = store.get(workspace, project_id)
    except DomainError:
        project = None
    if project is None or project["kind"] != "project" or project["archived"]:
        raise Failure("not_found", "No such project")
    return project


# -- reading


def _fits(declared, value, people):
    """Whether a stored value fits its field's declaration as it now stands."""
    kind = declared["type"]
    if value is None:
        return True
    if kind in {"text", "url"}:
        return isinstance(value, str)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "date":
        return plans.is_civil_date(value)
    if kind == "person":
        return isinstance(value, str) and value in people
    options = {o["id"] for o in declared["options"]}
    if kind == "option":
        return isinstance(value, str) and value in options
    return (
        isinstance(value, list) and all(isinstance(x, str) for x in value) and set(value) <= options
    )


def _item(task, view, members):
    """One task as a plan item. ``members`` are the IDs of the plan's items."""
    stage = plans.resolve_stage(task, view["stages"])
    dates = plans.plan_dates(task)
    milestones = {c["id"] for c in view["tasks"] if c["milestone"]}
    containers = {t["id"] for t in view["types"] if t["container"]}
    item = {
        "id": task["id"],
        "version": task["version"],
        "title": task["title"],
        "detail": task["description"] or None,
        "stage": stage["id"] if stage else None,
        "assignee": task["assignee_id"],
        "start": dates["start"],
        "end": dates["end"],
        "deadline": dates["deadline"],
        "priority": str(task["priority"]) if task["priority"] else None,
        "labels": list(task["label_ids"]),
        "dependsOn": [ref for ref in task["depends_on"] if ref in members],
        "parent": task["parent_id"] if task["parent_id"] in members else None,
        "milestone": task["milestone"],
        "inMilestone": task["milestone_id"] if task["milestone_id"] in milestones else None,
        "container": task["type"] in containers or task["id"] in view["parents"],
        "goal": task["goal_id"],
        "type": task["type"],
        "done": task["status"] == "completed",
        "estimate": task["estimate_minutes"],
        "url": None,
        "status": task["status"],
    }
    people = {p["id"] for p in view["people"]}
    for field in view["fields"]:
        value = task["values"].get(field["id"])
        # A value that no longer fits its field (retyped, option removed) is not
        # sent; the field lists the items it was left out for, in "omitted".
        item[CUSTOM + field["id"]] = value if _fits(field, value, people) else None
    return item


def _fields(view, writable):
    def field(id, label, type, **extra):
        return {"id": id, "label": label, "type": type, "writable": writable, **extra}

    def option(id, label, color=None, done=False):
        out = {"id": id, "label": label}
        if color:
            out["color"] = color
        if done:
            out["done"] = True
        return out

    people = [option(p["id"], p["title"] or p["id"]) for p in view["people"]]
    containers = [t for t in view["types"] if t["container"]]
    out = [
        field("title", "Title", "text"),
        field("detail", "Detail", "text"),
        field("stage", "Stage", "option", options=[
            option(s["id"], s["label"], s.get("color"), s["status"] == "completed")
            for s in view["stages"]]),
        field("assignee", "Owner", "option", options=people),
        field("start", "Start", "date"),
        field("end", "End", "date"),
        field("deadline", "Deadline", "date"),
        field("priority", "Priority", "option", options=PRIORITIES),
        field("labels", "Labels", "options",
              options=[option(x["id"], x["title"] or x["id"]) for x in view["labels"]]),
        field("dependsOn", "Depends on", "options", items=True),
        field("parent", containers[0]["label"] if containers else "Parent", "option", items=True),
        field("milestone", "Is a milestone", "bool"),
        field("inMilestone", "Milestone", "option", items=True, options=[
            option(c["id"], c["title"] or c["id"]) for c in view["tasks"] if c["milestone"]]),
        {"id": "container", "label": "Container", "type": "bool", "writable": False},
        field("goal", "Goal", "option",
              options=[option(g["id"], g["title"] or g["id"]) for g in view["goals"]]),
        field("done", "Done", "bool"),
        field("estimate", "Estimate (minutes)", "number"),
        {"id": "status", "label": "Status", "type": "option", "writable": False,
         "options": STATUSES},
    ]  # fmt: skip
    if view["types"]:
        out.append(
            field("type", "Type", "option",
                  options=[option(t["id"], t["label"], t.get("color")) for t in view["types"]])
        )  # fmt: skip
    for declared in view["fields"]:
        extra = {}
        if declared["type"] in {"option", "options"}:
            extra["options"] = [
                option(o["id"], o["label"], o.get("color")) for o in declared["options"]
            ]
        elif declared["type"] == "person":
            extra["options"] = people
        if declared.get("unit"):
            extra["unit"] = declared["unit"]
        person_ids = {p["id"] for p in view["people"]}
        omitted = [
            c["id"]
            for c in view["tasks"]
            if not _fits(declared, c["values"].get(declared["id"]), person_ids)
        ]
        if omitted:
            extra["omitted"] = omitted
        out.append(
            field(
                CUSTOM + declared["id"], declared["label"], FIELD_TYPES[declared["type"]], **extra
            )
        )
    return out


def read(store, ref):
    workspace, project_id = _ref(ref)
    _project(store, workspace, project_id)
    view = store.plan(workspace, project_id)
    project = view["project"]
    readonly = project["managed_by"] is not None
    members = {card["id"] for card in view["tasks"]}
    source = {
        "ref": ref,
        "title": project["title"] or project["id"],
        "description": project["description"],
        "readonly": readonly,
    }
    if readonly:
        source["managedBy"] = project["managed_by"]
    return {
        "plan": 1,
        "source": source,
        "cursor": str(view["cursor"]),
        "fields": _fields(view, not readonly),
        "items": [_item(card, view, members) for card in view["tasks"]],
    }


def listing(path):
    if path != ":memory:" and not Path(path).is_file():
        return {"sources": []}
    store = _open(path)
    try:
        sources = []
        for entry in store.workspaces():
            workspace = entry["workspace"]
            for project in store.list(workspace, kind="project"):
                description = (project["description"].splitlines() or [""])[0]
                sources.append(
                    {
                        "ref": f"{workspace}/{project['id']}",
                        "title": project["title"] or project["id"],
                        "description": description or f"Daymark project in workspace {workspace}",
                        "readonly": project["managed_by"] is not None,
                    }
                )
        return {"sources": sources}
    finally:
        store.close()


def changes(store, ref, after):
    workspace, project_id = _ref(ref)
    if not isinstance(after, str) or not re.fullmatch(r"[0-9]{1,18}", after):
        raise Failure("invalid", "The cursor is the string a read or changes returned")
    _project(store, workspace, project_id)
    result = store.changes(workspace, project_id, int(after))
    return {"cursor": str(result["cursor"]), "changed": result["changed"]}


# -- writing


def _translate(changes, task, members, view):
    """A ``set`` object as an entity payload. ``task`` is None for a create."""
    if not isinstance(changes, dict):
        raise Failure("invalid", "set is an object of field values")
    declared = {f["id"] for f in view["fields"]}
    out, dates, values = {}, {}, {}
    for key, value in changes.items():
        if key.startswith(CUSTOM) and key[len(CUSTOM) :] in declared:
            values[key[len(CUSTOM) :]] = value
            continue
        if key not in WRITABLE:
            raise Failure("invalid", f"{key} is not a writable field")
        if key == "title":
            if not isinstance(value, str):
                raise Failure("invalid", "title is text")
            out["title"] = value
        elif key == "detail":
            if value is not None and not isinstance(value, str):
                raise Failure("invalid", "detail is text or null")
            out["description"] = value or ""
        elif key in {"stage", "assignee", "parent", "inMilestone", "goal", "type"}:
            if value is not None and not isinstance(value, str):
                raise Failure("invalid", f"{key} is an option id or null")
            if key == "parent" and value is not None and value not in members:
                raise Failure("invalid", "parent must be an item of this plan")
            target = {"stage": "stage", "assignee": "assignee_id", "parent": "parent_id",
                      "inMilestone": "milestone_id", "goal": "goal_id", "type": "type"}[key]  # fmt: skip
            out[target] = value
        elif key in {"start", "end", "deadline"}:
            dates[key] = value
        elif key == "priority":
            if value is None:
                out["priority"] = 0
            elif str(value) in {"1", "2", "3", "4"} and not isinstance(value, bool):
                out["priority"] = int(value)
            else:
                raise Failure("invalid", "priority is 1, 2, 3, 4 or null")
        elif key in {"labels", "dependsOn"}:
            value = [] if value is None else value
            if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
                raise Failure("invalid", f"{key} is a list of ids")
            if key == "labels":
                out["label_ids"] = list(dict.fromkeys(value))
            else:
                if set(value) - members:
                    raise Failure("invalid", "dependsOn lists items of this plan only")
                # The plan shows only dependencies inside the project; keep the others.
                outside = [r for r in (task or {}).get("depends_on", []) if r not in members]
                out["depends_on"] = list(dict.fromkeys([*outside, *value]))
        elif key in {"milestone", "done"}:
            if not isinstance(value, bool):
                raise Failure("invalid", f"{key} is true or false")
            if key == "milestone":
                out["milestone"] = value
            elif value:
                out["status"] = "completed"
            elif task is None or task["status"] in {"completed", "cancelled"}:
                out["status"] = "open"
        elif key == "estimate":
            if value is not None and (type(value) is not int or value < 1):
                raise Failure("invalid", "estimate is a whole number of minutes or null")
            out["estimate_minutes"] = value
    if dates:
        # Only the dates this change sets are checked, and precision is kept.
        out.update(plans.schedule(task, timezone=local_timezone(), **dates))
    if values:
        out["values"] = values  # Daymark merges per field; null clears one.
    return _changed(out, task, view) if task is not None else out


def _changed(payload, task, view):
    """Only what differs from the task as it stands: a change that changes nothing
    writes nothing."""
    out = {}
    for key, value in payload.items():
        if key == "stage":
            current = plans.resolve_stage(task, view["stages"])
            if value is not None and current is not None and current["id"] == value:
                continue
        elif key == "values":
            merged = {**task["values"], **value}
            if {k: v for k, v in merged.items() if v is not None} == task["values"]:
                continue
            value = {k: v for k, v in value.items() if task["values"].get(k) != v}
        elif task.get(key) == value:
            continue
        out[key] = value
    return out


def _in_place(store, workspace, project_id, id, place):
    """Whether an item already sits where ``place`` would put it."""
    order = [c["id"] for c in store.plan(workspace, project_id)["tasks"]]
    if id not in order:
        return False
    i = order.index(id)
    key, ref = next(iter(place.items()))
    if key == "after":
        return order[i - 1] == ref if ref is not None and i > 0 else ref is None and i == 0
    if ref is None:
        return i == len(order) - 1
    return i + 1 < len(order) and order[i + 1] == ref


def _short_id(workspace, project_id, mutation):
    digest = int.from_bytes(
        hashlib.sha256(f"{workspace}\x00{project_id}\x00{mutation}".encode()).digest(), "big"
    )
    chars = []
    for _ in range(8):
        digest, index = divmod(digest, len(_ALPHABET))
        chars.append(_ALPHABET[index])
    return "dm-" + "".join(chars)


def _place(change):
    place = change.get("place")
    if place is None:
        return None
    if not isinstance(place, dict) or len(place) != 1 or set(place) - {"after", "before"}:
        raise Failure("invalid", 'place is {"after": ID or null}')
    value = next(iter(place.values()))
    if value is not None and not isinstance(value, str):
        raise Failure("invalid", "place names an item id or null")
    return place


def _apply(store, workspace, project_id, change, members, view, actor):
    """Run one change as one atomic batch, idempotent by its mutation within the
    project. Returns the item ID; raises Failure with the item ID to report."""
    if not isinstance(change, dict):
        raise Failure("invalid", "Each change is an object")
    mutation = change.get("mutation")
    if not isinstance(mutation, str) or not 1 <= len(mutation) <= 1000:
        raise Failure("invalid", "mutation is an idempotency key of 1 to 1000 characters")
    request_id = "toucan:" + hashlib.sha256(f"{project_id}\x00{mutation}".encode()).hexdigest()
    kind, place = change.get("op"), _place(change)
    if kind == "create":
        id = _short_id(workspace, project_id, mutation)
        payload = {
            "id": id,
            "kind": "task",
            "project_id": project_id,
            "title": "",
            **_translate(change.get("set") or {}, None, members, view),
        }
        operations = [{"operation_id": request_id + ":1", "action": "create",
                       "payload": payload, "expected_version": 0}]  # fmt: skip
        version = 1
    elif kind == "update":
        id, version = change.get("id"), change.get("version")
        if not isinstance(id, str) or type(version) is not int:
            raise Failure("invalid", "An update needs the item's id and version")
        try:
            task = store.get(workspace, id)
        except DomainError:
            task = None
        if task is None or task["kind"] != "task" or task["project_id"] != project_id:
            raise Failure("not_found", "No such item in this plan")
        if id not in members:
            # Proposed, archived, or cancelled off the board: not an item of the plan.
            raise Failure(
                "not_found", "Not an item of this plan (proposed, archived or off the board)"
            )
        try:
            payload = _translate(change.get("set") or {}, task, members, view)
        except DomainError as exc:
            raise Failure("invalid", str(exc), id) from None
        except Failure as failure:
            raise Failure(failure.code, str(failure), id) from None
        if place is not None and _in_place(store, workspace, project_id, id, place):
            place = None  # Already there.
        operations = []
        if payload:
            operations.append({"operation_id": request_id + ":1", "action": "update",
                               "payload": {"id": id, **payload}, "expected_version": version})  # fmt: skip
            version += 1
        if not operations and place is None:
            return id  # Nothing to change: nothing written, the item as it stands.
    else:
        raise Failure("invalid", "op is update or create")
    if place is not None:
        operations.append({"operation_id": request_id + ":2", "action": "place",
                           "payload": {"id": id, **place}, "expected_version": version})  # fmt: skip
    try:
        result = store.batch(
            workspace, operations, atomic=True, actor=actor,
            request_id=request_id, request={"project": project_id, "change": change},
        )  # fmt: skip
    except DomainError as exc:
        # The same mutation with a different change: refused like the engine refuses.
        code = exc.code if exc.code in CODES else "invalid"
        raise Failure(code, "Mutation reused with a different change", id) from None
    if not result["committed"]:
        error = next(r["error"] for r in result["results"] if r["error"]["code"] != "aborted")
        code = error["code"] if error["code"] in CODES else "invalid"
        raise Failure(code, error["message"] if code != "conflict" else "Changed upstream", id)
    members.add(id)  # A new item can be named by later changes in the same write.
    return result["results"][0]["result"]["id"]


def write(store, ref, document, actor):
    workspace, project_id = _ref(ref)
    project = _project(store, workspace, project_id)
    if project["managed_by"] is not None:
        raise Failure("forbidden", f"This project is managed by {project['managed_by']}")
    if not isinstance(document, dict) or not isinstance(document.get("changes"), list):
        raise Failure("invalid", 'The input is {"changes": [...]}')
    if len(document["changes"]) > 500:
        raise Failure("invalid", "At most 500 changes per write")
    view = store.plan(workspace, project_id)
    members = {card["id"] for card in view["tasks"]}
    outcomes = []
    for change in document["changes"]:
        mutation = change.get("mutation") if isinstance(change, dict) else None
        try:
            id = _apply(store, workspace, project_id, change, members, view, actor)
            outcomes.append((mutation, id, None))
        except Failure as failure:
            outcomes.append((mutation, failure.item_id, failure))
        except DomainError as exc:
            code = exc.code if exc.code in CODES else "invalid"
            fallback = change.get("id") if isinstance(change, dict) else None
            outcomes.append((mutation, fallback, Failure(code, str(exc))))
    view = store.plan(workspace, project_id)
    members = {card["id"] for card in view["tasks"]}
    results = []
    for mutation, id, failure in outcomes:
        result = {"mutation": mutation, "ok": failure is None}
        if failure is not None:
            result.update(error=failure.code, message=str(failure))
        task = None
        if isinstance(id, str):
            try:
                task = store.get(workspace, id)
            except DomainError:
                task = None
        if task is not None and task["kind"] == "task" and task["project_id"] == project_id:
            result["item"] = _item(task, view, members)
        results.append(result)
    return {"cursor": str(view["cursor"]), "results": results}


# -- entry point


def _parse(argv):
    """Options anywhere; ``--`` makes the next argument positional, so a reference can
    never be read as an option (toucan passes it before the reference)."""
    args, options = [], {}
    items = iter(argv)
    for arg in items:
        if arg == "--":
            following = next(items, None)
            if following is not None:
                args.append(following)
        elif arg in {"--db", "--after"}:
            options[arg[2:]] = next(items, None)
        elif arg.startswith(("--db=", "--after=")):
            name, _, value = arg[2:].partition("=")
            options[name] = value
        elif arg in {"-h", "--help"}:
            options["help"] = True
        elif arg == "--version":
            options["version"] = True
        elif arg.startswith("-"):
            raise Failure("invalid", "Unknown option; put -- before a reference")
        else:
            args.append(arg)
    return args, options


def run(argv, stdin):
    args, options = _parse(argv)
    if options.get("version"):
        from . import __version__

        return f"toucan-source-daymark {__version__}\n"
    if options.get("help") or not args:
        return __doc__
    command, rest = args[0], args[1:]
    path = database(options.get("db"))
    if command == "list" and not rest:
        return listing(path)
    if command not in {"read", "changes", "write"} or len(rest) != 1:
        raise Failure("invalid", "Usage: list | read REF | changes REF --after CUR | write REF")
    store = _open(path)
    try:
        if command == "read":
            return read(store, rest[0])
        if command == "changes":
            return changes(store, rest[0], options.get("after"))
        try:
            document = json.loads(stdin.read() or "null")
        except ValueError:
            raise Failure("invalid", "The input is not JSON") from None
        actor = os.environ.get("DAYMARK_ACTOR") or "toucan"
        return write(store, rest[0], document, actor)
    finally:
        store.close()


def main(argv=None, stdin=None, stdout=None, stderr=None):
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        result = run(sys.argv[1:] if argv is None else argv, stdin)
    except Failure as failure:
        stderr.write(json.dumps({"error": failure.code, "message": str(failure)}) + "\n")
        return CODES[failure.code]
    except DomainError as exc:
        code = exc.code if exc.code in CODES else "invalid"
        stderr.write(json.dumps({"error": code, "message": str(exc)}) + "\n")
        return CODES[code]
    except Exception:  # noqa: BLE001 - stored content must not reach the caller's logs
        stderr.write(json.dumps({"error": "unavailable", "message": "Daymark failed"}) + "\n")
        return CODES["unavailable"]
    if isinstance(result, str):
        stdout.write(result)
    else:
        stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
