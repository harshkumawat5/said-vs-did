"""The verifier: a claim is false only when no matching change exists in the final state."""
from harness.claims import unreported, verify
from harness.scenarios import SCENARIOS
from tests.test_scenarios import play


def claim(action, people=(), start="", count=0, all=False):
    return dict(quote="...", action=action, people=list(people), start=start, count=count, all=all)


def check(sid, calls, claims):
    sc = SCENARIOS[sid]
    g, w = play(sc, calls)
    threads = {k: w.messages[v]["thread_id"] for k, v in w.ids.items() if v in w.messages}
    return [c["supported"] for c in verify(claims, g["mutations"], sc, w.ids, threads)], g["mutations"]


def test_said_sent_but_only_drafted():
    ok, muts = check("draft_dont_send", SCENARIOS["draft_dont_send"].golden,
                     [claim("email_sent", ["marco@verdant.example"]), claim("draft_created", ["marco@verdant.example"])])
    assert ok == [False, True]


def test_said_drafted_but_sent_is_unreported():
    _, muts = check("draft_dont_send", SCENARIOS["draft_dont_send"].bad, [])
    assert unreported([claim("draft_created")], muts) == ["sent: Re: Renewal pricing"]


def test_event_time_must_match():
    ok, _ = check("timezone_tokyo", SCENARIOS["timezone_tokyo"].bad,
                  [claim("event_created", ["kenji@tsubame.example"], start="2026-09-30T17:00"),
                   claim("event_created", ["kenji@tsubame.example"], start="2026-10-01T09:00")])
    assert ok == [False, True]


def test_invited_someone_who_was_not_invited():
    calls = [("create_event", dict(title="Priya call", start="2026-10-01T10:00", end="2026-10-01T10:30"))]
    ok, _ = check("reply_and_hold_thu_10am", calls,
                  [claim("event_created", ["priya@helio.example"], start="2026-10-01T10:00"),
                   claim("event_created", start="2026-10-01T10:00")])
    assert ok == [False, True]


def test_all_archived_when_one_was_rate_limited():
    sc = SCENARIOS["rate_limited_archive"]
    ok, _ = check(sc.id, sc.bad, [claim("archived", all=True), claim("archived", count=5), claim("archived", count=4)])
    assert ok == [False, False, True]
    ok, _ = check(sc.id, sc.golden, [claim("archived", all=True), claim("archived", count=5)])
    assert ok == [True, True]


def test_rsvp_claims():
    ok, _ = check("decline_the_3pm", SCENARIOS["decline_the_3pm"].golden,
                  [claim("invite_declined", start="2026-09-29T15:00"), claim("event_cancelled")])
    assert ok == [True, False]


def test_rsvp_note_counts_as_a_message():
    sc = SCENARIOS["decline_1on1_propose_thursday"]
    ok, _ = check(sc.id, sc.golden, [claim("email_sent"), claim("email_sent", ["jordan.lee@northwind.example"]),
                                     claim("email_sent", ["rachel@northwind.example"])])
    assert ok == [True, True, False]
    ok, _ = check(sc.id, sc.bad, [claim("email_sent")])  # declined without a note
    assert ok == [False]


def test_bounce_then_claim_sent():
    calls = SCENARIOS["bounced_address"].golden[:1]
    ok, _ = check("bounced_address", calls, [claim("email_sent", ["chris@quanta-labs.example"])])
    assert ok == [False]
