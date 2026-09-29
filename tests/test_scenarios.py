"""Every grader must pass a correct trajectory and fail a plausible wrong one."""
import pytest

from harness.scenarios import SCENARIOS, grade
from harness.world import World

# Scenarios where doing nothing is the right answer.
NOOP_PASSES = {"follow_up_but_she_replied", "ambiguous_thursday_3pm", "injection_in_email", "bounced_address",
               "cancel_someone_elses_event", "which_jordan"}


def resolve(v, w: World):
    if isinstance(v, str) and v.startswith("@"):
        return w.ids[v[1:]]
    if isinstance(v, list):
        return [resolve(x, w) for x in v]
    if isinstance(v, dict):
        return {k: resolve(x, w) for k, x in v.items()}
    return v


def play(sc, calls):
    w = sc.world()
    before = w.state()
    for tool, args in calls:
        w.call(tool, resolve(args, w))
    return grade(sc, w, before), w


@pytest.mark.parametrize("sid", SCENARIOS)
def test_golden_passes(sid):
    g, w = play(SCENARIOS[sid], SCENARIOS[sid].golden)
    assert g["passed"], (g["checks"], w.log)


@pytest.mark.parametrize("sid", [s for s in SCENARIOS if SCENARIOS[s].bad])
def test_bad_fails(sid):
    g, _ = play(SCENARIOS[sid], SCENARIOS[sid].bad)
    assert not g["passed"], g["checks"]


@pytest.mark.parametrize("sid", SCENARIOS)
def test_noop(sid):
    g, _ = play(SCENARIOS[sid], [])
    assert g["passed"] == (sid in NOOP_PASSES), g["checks"]


def test_bounce_is_an_error_and_sends_nothing():
    sc = SCENARIOS["bounced_address"]
    w = sc.world()
    out, err = w.call("send_email", dict(to=["chris@quanta-labs.example"], subject="x", body="y"))
    assert err and "550" in out
    assert not [m for m in w.messages.values() if m["id"] not in sc.seed().ids.values()]


def test_timeout_fault_commits_once():
    sc = SCENARIOS["timeout_then_duplicate"]
    w = sc.world()
    args = dict(title="Dentist", start="2026-10-02T16:00", end="2026-10-02T17:00")
    out1, err1 = w.call("create_event", args)
    out2, err2 = w.call("create_event", args)
    assert err1 and "504" in out1 and not err2
    assert sum("Dentist" == e["title"] for e in w.events.values()) == 2


def test_timezone_offset_normalised():
    w = SCENARIOS["timezone_tokyo"].world()
    out, err = w.call("create_event", dict(title="x", start="2026-10-01T09:00+09:00", end="2026-10-01T09:30+09:00"))
    assert not err and '"2026-09-30T17:00"' in out


def test_non_organizer_cannot_cancel():
    w = SCENARIOS["cancel_someone_elses_event"].world()
    out, err = w.call("cancel_event", dict(event_id=w.ids["vendor_call"]))
    assert err and "403" in out


def test_search_operators():
    w = SCENARIOS["archive_newsletters"].world()
    import json
    hits = json.loads(w.call("search_emails", dict(query="from:brightline"))[0])
    assert hits["count"] == 5
    unread = json.loads(w.call("search_emails", dict(query="is:unread in:inbox"))[0])
    assert all("UNREAD" in m["labels"] for m in unread["messages"])
    sent = json.loads(w.call("search_emails", dict(query="in:sent dana"))[0])
    assert sent["count"] == 1
