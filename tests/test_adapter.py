"""toucan-source-daymark, run as a separate process through its installed entry point
against temporary databases. All records are synthetic.
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from daymark import Store, Tasks

NOW = datetime(2030, 4, 3, 15, 0, tzinfo=UTC)
ZONE = "Europe/London"
BOARD = "todo:To do:open:grey,doing:Doing:open:blue,done:Done:completed:green"
ADAPTER = Path(sys.executable).parent / "toucan-source-daymark"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "daymark.sqlite3"
    store = Store(path)
    tasks = Tasks(store, "home", actor="setup", timezone=ZONE, now=NOW)
    tasks.add("Ada", kind="person", id="ada")
    tasks.add("Urgent", kind="label", id="urgent")
    tasks.goal("Launch", id="launch", target="2030-06-01")
    tasks.project("Website relaunch", id="site", stages=BOARD, description="The new site",
                  types="epic:Epic:violet:container,story:Story:blue",
                  fields="points:Points:number,risk:Risk:option:low|high")  # fmt: skip
    tasks.add("Elsewhere", id="ext")
    tasks.add("Beta", id="beta", project="site", is_milestone=True, due="2030-05-01 17:30")
    tasks.add("Sitemap", id="map", project="site", plan="2030-04-10", target="2030-04-20",
              assignee="ada", after="ext", type="story", milestone="beta",
              values={"points": 3, "risk": "low"})  # fmt: skip
    tasks.add("Copy", id="copy", project="site", after="map", due="2030-05-01",
              start="2030-04-15")  # fmt: skip
    tasks.propose("Compare hosts", id="idea", project="site")
    tasks.project("Commitments", id="ledger-entries", managed_by="ledger")
    tasks.add("Projected", id="projected", project="ledger-entries")
    Tasks(store, "studio", timezone=ZONE, now=NOW).project("Album", id="album")
    store.close()
    return path


def run(db, *args, stdin=None, env=None):
    environment = {**os.environ, "DAYMARK_DB": str(db), "DAYMARK_TZ": ZONE, **(env or {})}
    process = subprocess.run(
        [str(ADAPTER), *args],
        input=json.dumps(stdin) if stdin is not None else "",
        capture_output=True,
        check=False,
        text=True,
        env=environment,
        timeout=60,
    )
    out = json.loads(process.stdout) if process.returncode == 0 else None
    err = json.loads(process.stderr) if process.returncode else None
    return process.returncode, out, err


def write(db, *changes):
    status, out, err = run(db, "write", "home/site", stdin={"changes": list(changes)})
    assert status == 0, err
    return out, {r["mutation"]: r for r in out["results"]}


def head(db):
    store = Store(db)
    try:
        return store.head("home")
    finally:
        store.close()


def task(db, id):
    store = Store(db)
    try:
        return store.get("home", id)
    finally:
        store.close()


def test_list_and_read(db):
    status, out, _ = run(db, "list")
    assert status == 0
    assert out == {"sources": [
        {"ref": "home/ledger-entries", "title": "Commitments", "readonly": True,
         "description": "Daymark project in workspace home"},
        {"ref": "home/site", "title": "Website relaunch", "readonly": False,
         "description": "The new site"},
        {"ref": "studio/album", "title": "Album", "readonly": False,
         "description": "Daymark project in workspace studio"},
    ]}  # fmt: skip
    status, plan, _ = run(db, "read", "home/site")
    assert status == 0 and plan["plan"] == 1 and plan["cursor"] == str(head(db))
    fields = {f["id"]: f for f in plan["fields"]}
    assert fields["stage"]["options"][-1] == {"id": "done", "label": "Done", "color": "green",
                                              "done": True}  # fmt: skip
    assert fields["parent"]["label"] == "Epic"
    assert fields["type"]["options"] == [{"id": "epic", "label": "Epic", "color": "violet"},
                                         {"id": "story", "label": "Story", "color": "blue"}]  # fmt: skip
    assert fields["inMilestone"]["options"] == [{"id": "beta", "label": "Beta"}]
    assert fields["inMilestone"]["items"] is True and fields["inMilestone"]["label"] == "Milestone"
    assert fields["milestone"]["type"] == "bool" and "isMilestone" not in fields
    assert fields["container"] == {
        "id": "container",
        "label": "Container",
        "type": "bool",
        "writable": False,
    }
    assert fields["goal"]["options"] == [{"id": "launch", "label": "Launch"}]
    assert fields["field:points"] == {"id": "field:points", "label": "Points", "type": "number",
                                      "writable": True}  # fmt: skip
    assert fields["field:risk"]["options"] == [{"id": "low", "label": "low"},
                                               {"id": "high", "label": "high"}]  # fmt: skip
    assert fields["status"]["writable"] is False
    items = {i["id"]: i for i in plan["items"]}
    assert list(items) == ["beta", "map", "copy"]  # Order; the proposal is not work yet.
    assert items["map"] == {
        "id": "map", "version": 1, "title": "Sitemap", "detail": None, "stage": "todo",
        "assignee": "ada", "start": "2030-04-10", "end": "2030-04-20", "deadline": None,
        "priority": None, "labels": [], "dependsOn": [], "parent": None, "milestone": False,
        "inMilestone": "beta", "container": False, "goal": None, "type": "story", "done": False, "estimate": None,
        "url": None, "status": "open", "field:points": 3, "field:risk": "low",
    }  # fmt: skip
    assert items["beta"]["milestone"] is True and items["copy"]["dependsOn"] == ["map"]
    assert (items["copy"]["start"], items["copy"]["end"]) == ("2030-04-15", "2030-05-01")
    plain = Store(db)
    Tasks(plain, "home", timezone=ZONE, now=NOW).project("Bare", id="bare")
    plain.close()
    bare = {f["id"] for f in run(db, "read", "home/bare")[1]["fields"]}
    assert "type" not in bare and not any(f.startswith("field:") for f in bare)


def test_write_changes_conflict_and_replay(db):
    cursor = run(db, "read", "home/site")[1]["cursor"]
    assert run(db, "changes", "home/site", "--after", cursor)[1] == {
        "cursor": cursor, "changed": False}  # fmt: skip
    changes = [
        {"op": "update", "id": "map", "version": 1, "mutation": "m-1",
         "set": {"stage": "done", "end": "2030-04-22", "labels": ["urgent"], "priority": "2",
                 "dependsOn": [], "field:points": 5, "field:risk": None, "goal": "launch"},
         "place": {"after": "copy"}},
        {"op": "create", "mutation": "m-2", "set": {"title": "New card", "stage": "doing",
                                                    "assignee": "ada", "estimate": 90,
                                                    "type": "epic", "inMilestone": "beta"},
         "place": {"after": None}},
        {"op": "update", "id": "copy", "version": 7, "mutation": "m-3", "set": {"title": "x"}},
        {"op": "update", "id": "copy", "version": 1, "mutation": "m-4",
         "set": {"url": "https://example.invalid"}},
        {"op": "update", "id": "nope", "version": 1, "mutation": "m-5", "set": {"title": "x"}},
        {"op": "update", "id": "copy", "version": 1, "mutation": "m-6",
         "set": {"field:points": "many"}},
    ]  # fmt: skip
    out, results = write(db, *changes)
    item = results["m-1"]["item"]
    assert results["m-1"]["ok"] and item["stage"] == "done" and item["done"] is True
    assert (item["start"], item["end"], item["goal"]) == ("2030-04-10", "2030-04-22", "launch")
    assert item["field:points"] == 5 and item["field:risk"] is None
    assert item["labels"] == ["urgent"] and item["priority"] == "2"
    created = results["m-2"]["item"]
    assert (created["stage"], created["type"], created["inMilestone"]) == ("doing", "epic", "beta")
    assert results["m-3"]["error"] == "conflict" and results["m-3"]["item"]["version"] == 1
    assert results["m-4"]["error"] == "invalid" and results["m-4"]["item"]["id"] == "copy"
    assert results["m-5"]["error"] == "not_found" and "item" not in results["m-5"]
    assert results["m-6"]["error"] == "invalid" and "number" in results["m-6"]["message"]
    assert out["cursor"] == str(head(db))
    after = run(db, "read", "home/site")[1]
    assert [i["id"] for i in after["items"]] == [created["id"], "beta", "copy", "map"]
    stored = task(db, "map")
    assert stored["depends_on"] == ["ext"]  # The dependency outside the plan survives.
    assert stored["deadline"] is None and stored["values"] == {"points": 5}
    store = Store(db)
    assert {e["actor"] for e in store.events("home", after=int(cursor))} == {"toucan"}
    # Placing a card wrote only that card: copy and beta kept their versions.
    assert store.get("home", "copy")["version"] == 1 and store.get("home", "beta")["version"] == 1
    store.close()
    assert run(db, "changes", "home/site", "--after", cursor)[1]["changed"] is True
    # Replaying the same changes writes nothing and answers with the current items.
    again, replayed = write(db, *changes)
    assert again["cursor"] == after["cursor"]
    assert replayed["m-1"]["ok"] and replayed["m-1"]["item"] == item
    assert replayed["m-2"]["item"]["id"] == created["id"]
    # The same mutation with a different change is a conflict, not a silent replay.
    _, reused = write(db, {**changes[0], "set": {"title": "Something else"}})
    assert reused["m-1"]["error"] == "conflict" and task(db, "map")["title"] == "Sitemap"


def test_mutations_are_scoped_to_their_project(db):
    store = Store(db)
    Tasks(store, "home", timezone=ZONE, now=NOW).project("Other", id="other")
    store.close()
    change = {"op": "create", "mutation": "same", "set": {"title": "Here"}}
    _, first = write(db, change)
    status, out, _ = run(db, "write", "home/other", stdin={"changes": [change]})
    other = out["results"][0]
    assert status == 0 and other["ok"] and other["item"]["id"] != first["same"]["item"]["id"]
    assert task(db, other["item"]["id"])["project_id"] == "other"


def test_dates_only_checked_when_written_and_deadlines_keep_their_time(db):
    # Copy starts 2030-04-15 (not before) and ends at its deadline 2030-05-01; push its
    # plan past the deadline through Daymark itself, then edit only the title.
    store = Store(db)
    Tasks(store, "home", timezone=ZONE, now=NOW).edit("copy", plan="2030-05-10")
    store.close()
    _, results = write(db, {"op": "update", "id": "copy", "version": 2, "mutation": "t",
                            "set": {"title": "Copy v2"}})  # fmt: skip
    assert results["t"]["ok"] and results["t"]["item"]["title"] == "Copy v2"
    _, results = write(db, {"op": "update", "id": "copy", "version": 3, "mutation": "e",
                            "set": {"end": "2030-05-02"}})  # fmt: skip
    assert (
        results["e"]["error"] == "invalid" and "start is after the end" in results["e"]["message"]
    )
    # Writing a timed deadline's own date back leaves it, and its time, untouched.
    _, results = write(db, {"op": "update", "id": "beta", "version": 1, "mutation": "d",
                            "set": {"deadline": "2030-05-01", "title": "Beta!"}})  # fmt: skip
    assert task(db, "beta")["deadline"]["at"].startswith("2030-05-01T17:30")
    _, results = write(db, {"op": "update", "id": "beta", "version": 2, "mutation": "d2",
                            "set": {"deadline": "2030-05-03"}})  # fmt: skip
    assert task(db, "beta")["deadline"]["at"].startswith("2030-05-03T17:30")
    # Moving the plan's end never moves the deadline.
    _, results = write(db, {"op": "update", "id": "beta", "version": 3, "mutation": "d3",
                            "set": {"end": "2030-04-30"}})  # fmt: skip
    assert results["d3"]["item"]["deadline"] == "2030-05-03"
    assert results["d3"]["item"]["end"] == "2030-04-30"


def test_read_only_projects_refuse_writes(db):
    status, plan, _ = run(db, "read", "home/ledger-entries")
    assert plan["source"]["readonly"] is True and plan["source"]["managedBy"] == "ledger"
    assert not any(f["writable"] for f in plan["fields"])
    status, _, err = run(db, "write", "home/ledger-entries", stdin={"changes": [
        {"op": "update", "id": "projected", "version": 1, "mutation": "r-1",
         "set": {"title": "Edited"}}]})  # fmt: skip
    assert status == 7 and err["error"] == "forbidden"
    assert task(db, "projected")["title"] == "Projected"


def test_failures_are_coded_json_on_standard_error(db, tmp_path):
    assert run(db, "read", "home/missing")[0:3:2] == (3, {"error": "not_found",
                                                          "message": "No such project"})  # fmt: skip
    assert run(db, "read", "studio/site")[2]["error"] == "not_found"  # Workspaces stay apart.
    assert run(db, "read", "no-slash")[2]["error"] == "invalid"
    assert run(db, "changes", "home/site", "--after", "x")[2]["error"] == "invalid"
    assert run(db, "changes", "home/site")[2]["error"] == "invalid"
    assert run(db, "frobnicate")[2]["error"] == "invalid"
    assert run(db, "write", "home/site", stdin=[1])[2]["error"] == "invalid"
    _, out, _ = run(db, "write", "home/site", stdin={"changes": [{"op": "delete"}]})
    assert out["results"][0]["error"] == "invalid"
    missing = tmp_path / "absent.sqlite3"
    assert run(missing, "list") == (0, {"sources": []}, None)
    assert run(missing, "read", "home/site")[2]["error"] == "unavailable"
    assert not missing.exists()  # The adapter never creates a database.
    help_text = subprocess.run([str(ADAPTER)], capture_output=True, check=False, text=True)
    assert "toucan.plan/1" in help_text.stdout


def test_an_older_database_is_reported_not_upgraded(db, tmp_path):
    old = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(old)
    connection.executescript((Path(__file__).parent / "fixtures" / "daymark-0.3.0.sql").read_text())
    connection.commit()
    connection.close()
    for args in (("list",), ("read", "home/garden"), ("changes", "home/garden", "--after", "0")):
        status, _, err = run(old, *args)
        assert status == 1 and err["error"] == "unavailable" and "0.4.0" in err["message"]
    check = sqlite3.connect(old)
    assert check.execute("SELECT value FROM daymark_scheduler WHERE key='schema'").fetchone() == (
        "4",
    )
    check.close()


def test_the_entry_point_reports_its_version():
    out = subprocess.run([str(ADAPTER), "--version"], capture_output=True, check=False, text=True)
    assert out.stdout.strip() == "toucan-source-daymark 0.1.0"


def test_milestone_marker_membership_and_containers(db):
    store = Store(db)
    tasks = Tasks(store, "home", timezone=ZONE, now=NOW)
    tasks.add("Accounts", id="acc", project="site", type="epic")
    tasks.add("Parent by use", id="holder", project="site")
    tasks.add("Child", id="child", project="site", parent="holder")
    store.close()
    items = {i["id"]: i for i in run(db, "read", "home/site")[1]["items"]}
    assert items["acc"]["container"] is True  # A container type, even with no children.
    assert items["holder"]["container"] is True  # Has children.
    assert items["child"]["container"] is False and items["map"]["container"] is False
    _, results = write(
        db,
        {"op": "update", "id": "copy", "version": 1, "mutation": "mark",
         "set": {"milestone": True}},
        {"op": "update", "id": "child", "version": 1, "mutation": "join",
         "set": {"inMilestone": "beta"}},
        {"op": "update", "id": "map", "version": 1, "mutation": "box",
         "set": {"container": True}},
    )  # fmt: skip
    assert results["mark"]["item"]["milestone"] is True and task(db, "copy")["milestone"] is True
    assert results["join"]["item"]["inMilestone"] == "beta"
    assert results["box"]["error"] == "invalid"  # Read-only.


def test_double_dash_before_a_reference(db):
    cursor = run(db, "read", "--", "home/site")[1]["cursor"]
    assert run(db, "changes", "--", "home/site", "--after", cursor)[1]["changed"] is False
    assert run(db, "changes", "--after", cursor, "--", "home/site")[1]["changed"] is False
    assert run(db, "--db", str(db), "read", "--", "home/site")[0] == 0
    status, _, err = run(db, "write", "--", "home/site", stdin={"changes": []})
    assert status == 0
    for args in (("read", "--", "-home/site"), ("read", "--", "--help"), ("read", "-x")):
        status, _, err = run(db, *args)
        assert (status, err["error"]) == (5, "invalid")


def test_a_write_killed_midway_leaves_whole_changes_only(db):
    """Each change is one SQLite transaction (create and place together), so killing
    the process group mid-write leaves every change applied whole or not at all; a
    retry of the same write completes it without duplicates."""
    import signal
    import time

    changes = [
        {"op": "create", "mutation": f"bulk-{i}", "set": {"title": f"Card {i}", "field:points": i},
         "place": {"after": None}}
        for i in range(400)
    ]  # fmt: skip
    payload = json.dumps({"changes": changes})
    process = subprocess.Popen(
        [str(ADAPTER), "write", "--", "home/site"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "DAYMARK_DB": str(db), "DAYMARK_TZ": ZONE},
        start_new_session=True,
    )
    process.stdin.write(payload.encode())
    process.stdin.close()
    time.sleep(1.0)
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=30)
    store = Store(db)
    assert store.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    made = [t for t in store.list("home", kind="task") if t["title"].startswith("Card ")]
    assert all(t["rank"] and t["values"]["points"] == int(t["title"][5:]) for t in made)
    store.close()
    status, out, _ = run(db, "write", "--", "home/site", stdin={"changes": changes})
    assert status == 0 and all(r["ok"] for r in out["results"])
    store = Store(db)
    titles = [t["title"] for t in store.list("home", kind="task") if t["title"].startswith("Card ")]
    store.close()
    assert sorted(titles) == sorted(f"Card {i}" for i in range(400))


# -- second review


@pytest.fixture
def typed(tmp_path):
    """A project using every custom field type."""
    path = tmp_path / "typed.sqlite3"
    store = Store(path)
    tasks = Tasks(store, "home", timezone=ZONE, now=NOW)
    tasks.add("Ada", kind="person", id="ada")
    tasks.project("Typed", id="typed", fields=(
        "note:Note:text,link:Link:url,flag:Flag:bool,when:When:date,tags:Tags:options:a|b|c,"
        "who:Who:person,risk:Risk:option:lo|hi,pts:Points:number"))  # fmt: skip
    tasks.add("One", id="one", project="typed")
    tasks.add("Two", id="two", project="typed")
    tasks.propose("Idea", id="idea", project="typed")
    tasks.add("Shelved", id="shelved", project="typed")
    tasks.archive("shelved")
    tasks.add("Dropped", id="dropped", project="typed")
    tasks.cancel("dropped")
    tasks.project("Old", id="old")
    tasks.archive("old")
    store.close()
    return path


def write_to(db, ref, *changes):
    status, out, err = run(db, "write", "--", ref, stdin={"changes": list(changes)})
    assert status == 0, err
    return {r["mutation"]: r for r in out["results"]}


def test_every_custom_field_type_reads_and_writes(typed):
    good = {"field:note": "Call first", "field:link": "https://example.invalid/spec",
            "field:flag": True, "field:when": "2030-04-30", "field:tags": ["a", "c"],
            "field:who": "ada", "field:risk": "hi", "field:pts": 2.5}  # fmt: skip
    fields = {f["id"]: f for f in run(typed, "read", "--", "home/typed")[1]["fields"]}
    assert [fields[k]["type"] for k in good] == [
        "text", "text", "bool", "date", "options", "option", "option", "number"]  # fmt: skip
    assert fields["field:who"]["options"] == [{"id": "ada", "label": "Ada"}]
    results = write_to(typed, "home/typed", {"op": "update", "id": "one", "version": 1,
                                             "mutation": "all", "set": good})  # fmt: skip
    assert results["all"]["ok"], results
    assert {k: results["all"]["item"][k] for k in good} == good
    for key, bad in (("field:link", "ftp://x"), ("field:flag", "yes"), ("field:when", "30/04"),
                     ("field:tags", ["z"]), ("field:who", "nobody"), ("field:pts", "many")):  # fmt: skip
        result = write_to(typed, "home/typed", {"op": "update", "id": "two", "version": 1,
                                                "mutation": "bad" + key, "set": {key: bad}})  # fmt: skip
        assert result["bad" + key]["error"] == "invalid", key
    cleared = write_to(typed, "home/typed", {"op": "update", "id": "one", "version": 2,
                                             "mutation": "clear", "set": {"field:note": None}})  # fmt: skip
    assert cleared["clear"]["item"]["field:note"] is None
    assert cleared["clear"]["item"]["field:tags"] == ["a", "c"]  # Other values stay.


def test_values_that_no_longer_fit_their_field_are_omitted_and_marked(typed):
    write_to(typed, "home/typed", {"op": "update", "id": "one", "version": 1, "mutation": "v",
                                   "set": {"field:pts": 3, "field:risk": "hi"}})  # fmt: skip
    store = Store(typed)
    Tasks(store, "home", timezone=ZONE, now=NOW).configure(
        "typed", fields="pts:Points:text,risk:Risk:option:lo"
    )
    store.close()
    plan = run(typed, "read", "--", "home/typed")[1]
    fields = {f["id"]: f for f in plan["fields"]}
    item = next(i for i in plan["items"] if i["id"] == "one")
    assert item["field:pts"] is None and fields["field:pts"]["omitted"] == ["one"]
    assert item["field:risk"] is None and fields["field:risk"]["omitted"] == ["one"]
    assert task(typed, "one")["values"] == {"pts": 3, "risk": "hi"}  # Kept in Daymark.


def test_writes_reach_only_items_of_the_plan(typed):
    for id in ("idea", "shelved", "dropped"):
        result = write_to(typed, "home/typed", {"op": "update", "id": id,
                                                "version": task(typed, id)["version"],
                                                "mutation": id, "set": {"title": "x"}})  # fmt: skip
        assert result[id]["error"] == "not_found" and "item" not in result[id]
        assert task(typed, id)["title"] != "x"
    store = Store(typed)
    Tasks(store, "home", timezone=ZONE, now=NOW).add("Elsewhere", id="loose")
    store.close()
    results = write_to(
        typed, "home/typed",
        {"op": "update", "id": "one", "version": 1, "mutation": "dep",
         "set": {"dependsOn": ["loose"]}},
        {"op": "update", "id": "one", "version": 1, "mutation": "par",
         "set": {"parent": "idea"}},
        {"op": "create", "mutation": "new", "set": {"title": "New"}},
        {"op": "update", "id": "one", "version": 1, "mutation": "later",
         "set": {"dependsOn": [task_id("typed", "new")]}},
    )  # fmt: skip
    assert results["dep"]["error"] == "invalid" and results["par"]["error"] == "invalid"
    assert results["later"]["ok"]  # An item created earlier in the same write counts.
    assert task(typed, "one")["depends_on"] == [task_id("typed", "new")]
    assert run(typed, "read", "--", "home/old")[2]["error"] == "not_found"
    assert "home/old" not in [s["ref"] for s in run(typed, "list")[1]["sources"]]


def task_id(project, mutation):
    from toucan_source_daymark.adapter import _short_id

    return _short_id("home", project, mutation)


def test_a_change_that_changes_nothing_writes_nothing(db):
    before = head(db)
    results = write_to(db, "home/site", {
        "op": "update", "id": "map", "version": 1, "mutation": "same",
        "set": {"title": "Sitemap", "stage": "todo", "field:points": 3, "start": "2030-04-10",
                "inMilestone": "beta"}})  # fmt: skip
    assert results["same"]["ok"] and results["same"]["item"]["version"] == 1
    results = write_to(db, "home/site", {"op": "update", "id": "map", "version": 1,
                                         "mutation": "here", "place": {"after": "beta"}})  # fmt: skip
    assert results["here"]["ok"] and results["here"]["item"]["version"] == 1
    results = write_to(db, "home/site", {"op": "update", "id": "map", "version": 0,
                                         "mutation": "stale-noop", "set": {}})  # fmt: skip
    assert results["stale-noop"]["ok"] and results["stale-noop"]["item"]["version"] == 1
    assert head(db) == before


def test_a_null_start_clears_and_a_null_end_leaves_the_deadline(db):
    results = write_to(db, "home/site", {"op": "update", "id": "copy", "version": 1,
                                         "mutation": "s", "set": {"start": None}})  # fmt: skip
    assert results["s"]["item"]["start"] is None and task(db, "copy")["earliest_start"] is None
    results = write_to(db, "home/site", {"op": "update", "id": "copy", "version": 2,
                                         "mutation": "e", "set": {"end": None}})  # fmt: skip
    assert results["e"]["item"]["end"] == results["e"]["item"]["deadline"] == "2030-05-01"


def test_a_busy_database_answers_unavailable_inside_the_allowance(db):
    import time

    holder = sqlite3.connect(db, timeout=1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE daymark_scheduler SET value=value WHERE key='schema'")
    try:
        assert run(db, "changes", "--", "home/site", "--after", "0")[0] == 0  # Reads go on.
        started = time.monotonic()
        status, _, err = run(db, "write", "--", "home/site", stdin={"changes": [
            {"op": "create", "mutation": "blocked", "set": {"title": "x"}}]})  # fmt: skip
        took = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert status == 1 and err["error"] == "unavailable" and "busy" in err["message"]
    assert 9 < took < 25


def test_changes_answer_while_another_process_writes(db):
    changes = [{"op": "create", "mutation": f"c{i}", "set": {"title": f"Card {i}"}}
               for i in range(300)]  # fmt: skip
    import threading

    writer = subprocess.Popen(
        [str(ADAPTER), "write", "--", "home/site"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "DAYMARK_DB": str(db), "DAYMARK_TZ": ZONE},
    )  # fmt: skip
    box = {}
    talker = threading.Thread(
        target=lambda: box.update(out=writer.communicate(json.dumps({"changes": changes}))[0])
    )
    talker.start()
    polls = []
    while talker.is_alive() and len(polls) < 200:
        polls.append(run(db, "changes", "--", "home/site", "--after", "0"))
    talker.join(timeout=120)
    assert writer.returncode == 0
    assert polls and all(status == 0 for status, _, _ in polls)
    assert all(r["ok"] for r in json.loads(box["out"])["results"])


def test_a_newer_schema_is_refused(db):
    raw = sqlite3.connect(db)
    raw.execute("UPDATE daymark_scheduler SET value='6' WHERE key='schema'")
    raw.commit()
    raw.close()
    status, _, err = run(db, "read", "--", "home/site")
    assert status == 1 and err["error"] == "unavailable" and "newer Daymark" in err["message"]


def test_a_daymark_too_old_for_this_adapter_is_named(tmp_path):
    fake = tmp_path / "fake" / "daymark"
    fake.mkdir(parents=True)
    (fake / "__init__.py").write_text("class DomainError(Exception):\n    pass\nStore = None\n")
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(fake.parent)!r})\n"
        "from toucan_source_daymark.adapter import main\n"
        "raise SystemExit(main(['read', '--', 'home/site']))\n"
    )
    process = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                             check=False)  # fmt: skip
    error = json.loads(process.stderr)
    assert process.returncode == 1 and error["error"] == "unavailable"
    assert "needs daymark-core 0.4.0" in error["message"]
