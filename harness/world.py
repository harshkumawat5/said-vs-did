"""In-memory Gmail + Calendar for one user, plus the tools an agent calls on it.

All state is plain dicts so a run can be snapshotted, diffed and written to JSON.
Times are naive ISO strings in the user's timezone (America/Los_Angeles).
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 29, 9, 0)  # Tuesday
USER = "sam@northwind.example"
USER_NAME = "Sam Rivera"
EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def oid(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


class ToolError(Exception):
    pass


def parse_dt(s: str) -> datetime:
    """ISO 8601 -> naive local datetime. Offsets are converted to the user's timezone."""
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"invalid datetime {s!r}: use ISO 8601, e.g. 2026-10-01T10:00")
    if dt.tzinfo:
        dt = dt.astimezone(TZ).replace(tzinfo=None)
    return dt.replace(second=0, microsecond=0)


def parse_date(s: str) -> date:
    try:
        return date.fromisoformat(str(s).strip()[:10])
    except ValueError:
        raise ToolError(f"invalid date {s!r}: use YYYY-MM-DD")


def _addresses(v: Any, field: str) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        v = [p for p in re.split(r"[,;]", v) if p.strip()]
    out = []
    for a in v:
        m = re.search(r"<([^>]+)>", a)
        a = (m.group(1) if m else a).strip().lower()
        if not EMAIL_RE.match(a):
            raise ToolError(f"{field}: {a!r} is not an email address")
        out.append(a)
    return out


@dataclass
class Fault:
    """Make a tool call fail. `commit=True` performs the write first (a timeout after the write landed)."""

    tool: str
    error: str
    match: Callable[[dict], bool] = lambda args: True
    nth: int | None = None  # fire only on the nth matching call (1-based); None = every matching call
    commit: bool = False
    _seen: int = 0


class Seed:
    """Builds the starting state. Scenario code refers to items by readable keys; the agent only sees opaque ids."""

    def __init__(self) -> None:
        self.people: dict[str, str] = {USER: USER_NAME}
        self.messages: list[dict] = []
        self.drafts: list[dict] = []
        self.events: list[dict] = []
        self.ids: dict[str, str] = {}
        self.bounces: dict[str, str] = {}  # address -> SMTP error returned when mail is sent to it

    def person(self, email: str, name: str) -> None:
        self.people[email] = name

    def _thread(self, key: str, thread: str | None) -> str:
        if thread is None:
            return oid("thr", key)
        return next(m["thread_id"] for m in self.messages if m["id"] == self.ids[thread])

    def email(self, key, frm, subject, body, when, to=(USER,), cc=(), unread=True, thread=None, archived=False):
        labels = [] if archived else ["INBOX"]
        if unread:
            labels.append("UNREAD")
        mid = self.ids[key] = oid("msg", key)
        self.messages.append(dict(id=mid, thread_id=self._thread(key, thread), **{"from": frm}, to=list(to), cc=list(cc),
                                  subject=subject, body=body, date=when, labels=labels))
        return mid

    def sent(self, key, to, subject, body, when, cc=(), thread=None):
        mid = self.ids[key] = oid("msg", key)
        self.messages.append(dict(id=mid, thread_id=self._thread(key, thread), **{"from": USER}, to=list(to), cc=list(cc),
                                  subject=subject, body=body, date=when, labels=["SENT"]))
        return mid

    def draft(self, key, to, subject, body, cc=(), reply_to=None):
        did = self.ids[key] = oid("dft", key)
        thread = self._thread(key, reply_to) if reply_to else None
        self.drafts.append(dict(id=did, to=list(to), cc=list(cc), subject=subject, body=body,
                                reply_to_message_id=self.ids.get(reply_to), thread_id=thread))
        return did

    def event(self, key, title, start, end, organizer=USER, attendees=(), description="", location="", my_response=None):
        """attendees: other people. The user is added as organizer or as an invitee with `my_response`."""
        att = {a: "accepted" for a in attendees}
        if organizer != USER:
            att[USER] = my_response or "accepted"
        eid = self.ids[key] = oid("evt", key)
        self.events.append(dict(id=eid, title=title, start=start, end=end, organizer=organizer, attendees=att,
                                description=description, location=location, status="confirmed", my_comment=""))
        return eid


class World:
    def __init__(self, seed: Seed, faults: list[Fault] | None = None) -> None:
        self.people = dict(seed.people)
        self.ids = dict(seed.ids)
        self.messages = {m["id"]: copy.deepcopy(m) for m in seed.messages}
        self.drafts = {d["id"]: copy.deepcopy(d) for d in seed.drafts}
        self.events = {e["id"]: copy.deepcopy(e) for e in seed.events}
        self.followups: dict[str, dict] = {}
        self.bounces = dict(seed.bounces)
        self.faults = [copy.copy(f) for f in (faults or [])]
        self.log: list[dict] = []
        self._n = 0
        self._clock = NOW

    # ---- plumbing -------------------------------------------------------

    def state(self) -> dict:
        return copy.deepcopy(dict(messages=self.messages, drafts=self.drafts, events=self.events, followups=self.followups))

    def _new(self, prefix: str) -> str:
        self._n += 1
        return oid(prefix, f"new-{prefix}-{self._n}")

    def _tick(self) -> str:
        self._clock += timedelta(minutes=1)
        return fmt(self._clock)

    def _who(self, email: str) -> str:
        name = self.people.get(email)
        return f"{name} <{email}>" if name else email

    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run a tool. Returns (result text, is_error)."""
        fn = getattr(self, "tool_" + name, None)
        if fn is None:
            out, err = f"unknown tool {name!r}", True
        else:
            fault = None
            for f in self.faults:
                if f.tool == name and f.match(args):
                    f._seen += 1
                    if f.nth is None or f._seen == f.nth:
                        fault = f
                        break
            try:
                if fault and not fault.commit:
                    raise ToolError(fault.error)
                result = fn(**args)
                if fault:
                    raise ToolError(fault.error)
                out, err = json.dumps(result, indent=1), False
            except ToolError as e:
                out, err = f"Error: {e}", True
            except TypeError as e:  # bad/missing arguments
                out, err = f"Error: invalid arguments: {e}", True
        self.log.append(dict(tool=name, args=args, result=out, is_error=err))
        return out, err

    # ---- Gmail ----------------------------------------------------------

    def _summary(self, m: dict) -> dict:
        return dict(id=m["id"], thread_id=m["thread_id"], date=m["date"], **{"from": self._who(m["from"])},
                    to=[self._who(a) for a in m["to"]], subject=m["subject"],
                    snippet=re.sub(r"\s+", " ", m["body"])[:120], labels=m["labels"])

    def tool_search_emails(self, query: str = "", max_results: int = 20):
        try:
            toks = shlex.split(query)
        except ValueError:
            toks = query.split()
        scope, flt, words = "anywhere", [], []
        for t in toks:
            k, _, v = t.partition(":")
            k, v = k.lower(), v.lower()
            if not v or k not in {"from", "to", "subject", "is", "in", "after", "before", "label"}:
                words.append(t.lower())
            elif k == "in":
                scope = v
            elif k == "label":
                scope = "inbox" if v == "inbox" else scope
            else:
                flt.append((k, v))

        def full(addr):
            return f"{addr} {self.people.get(addr, '')}".lower()

        def ok(m):
            labels = m["labels"]
            if scope == "inbox" and "INBOX" not in labels:
                return False
            if scope == "sent" and "SENT" not in labels:
                return False
            if scope == "archive" and ("INBOX" in labels or "SENT" in labels):
                return False
            for k, v in flt:
                if k == "from" and v not in full(m["from"]):
                    return False
                if k == "to" and not any(v in full(a) for a in m["to"] + m["cc"]):
                    return False
                if k == "subject" and v not in m["subject"].lower():
                    return False
                if k == "is" and v == "unread" and "UNREAD" not in labels:
                    return False
                if k == "is" and v == "read" and "UNREAD" in labels:
                    return False
                if k == "after" and m["date"][:10] <= v.replace("/", "-"):
                    return False
                if k == "before" and m["date"][:10] >= v.replace("/", "-"):
                    return False
            hay = " ".join([full(m["from"]), *map(full, m["to"] + m["cc"]), m["subject"], m["body"]]).lower()
            return all(w in hay for w in words)

        hits = sorted((m for m in self.messages.values() if ok(m)), key=lambda m: m["date"], reverse=True)
        return dict(count=len(hits), messages=[self._summary(m) for m in hits[: max(1, int(max_results))]])

    def _msg(self, message_id: str) -> dict:
        m = self.messages.get(message_id)
        if not m:
            raise ToolError(f"message {message_id!r} not found")
        return m

    def _full(self, m: dict) -> dict:
        return dict(self._summary(m), cc=[self._who(a) for a in m["cc"]], body=m["body"])

    def tool_read_email(self, message_id: str):
        m = self._msg(message_id)
        if "UNREAD" in m["labels"]:
            m["labels"].remove("UNREAD")
        out = self._full(m)
        out.pop("snippet")
        return out

    def tool_read_thread(self, thread_id: str):
        ms = sorted((m for m in self.messages.values() if m["thread_id"] == thread_id), key=lambda m: m["date"])
        if not ms:
            raise ToolError(f"thread {thread_id!r} not found")
        for m in ms:
            if "UNREAD" in m["labels"]:
                m["labels"].remove("UNREAD")
        return dict(thread_id=thread_id, messages=[{k: v for k, v in self._full(m).items() if k != "snippet"} for m in ms])

    def _compose(self, to, subject, body, cc, reply_to_message_id):
        to, cc = _addresses(to, "to"), _addresses(cc, "cc")
        if not to:
            raise ToolError("at least one recipient is required in 'to'")
        thread = None
        if reply_to_message_id:
            thread = self._msg(reply_to_message_id)["thread_id"]
        return to, cc, str(subject), str(body), thread

    def tool_create_draft(self, to, subject, body, cc=None, reply_to_message_id=None):
        to, cc, subject, body, thread = self._compose(to, subject, body, cc, reply_to_message_id)
        did = self._new("dft")
        self.drafts[did] = dict(id=did, to=to, cc=cc, subject=subject, body=body,
                                reply_to_message_id=reply_to_message_id, thread_id=thread)
        return dict(draft_id=did, status="draft saved, not sent")

    def tool_list_drafts(self):
        return dict(drafts=[dict(d, to=[self._who(a) for a in d["to"]], cc=[self._who(a) for a in d["cc"]])
                            for d in self.drafts.values()])

    def _deliver(self, to, cc, subject, body, thread) -> dict:
        for a in to + cc:
            if a in self.bounces:
                raise ToolError(self.bounces[a])
        mid = self._new("msg")
        m = dict(id=mid, thread_id=thread or oid("thr", mid), **{"from": USER}, to=to, cc=cc,
                 subject=subject, body=body, date=self._tick(), labels=["SENT"])
        self.messages[mid] = m
        return m

    def tool_send_email(self, to, subject, body, cc=None, reply_to_message_id=None):
        to, cc, subject, body, thread = self._compose(to, subject, body, cc, reply_to_message_id)
        m = self._deliver(to, cc, subject, body, thread)
        return dict(message_id=m["id"], thread_id=m["thread_id"], status="sent")

    def tool_send_draft(self, draft_id: str):
        d = self.drafts.get(draft_id)
        if not d:
            raise ToolError(f"draft {draft_id!r} not found")
        m = self._deliver(d["to"], d["cc"], d["subject"], d["body"], d["thread_id"])
        del self.drafts[draft_id]
        return dict(message_id=m["id"], thread_id=m["thread_id"], status="sent")

    def tool_archive(self, message_id: str):
        thread = self._msg(message_id)["thread_id"]
        n = 0
        for m in self.messages.values():
            if m["thread_id"] == thread and "INBOX" in m["labels"]:
                m["labels"].remove("INBOX")
                n += 1
        return dict(thread_id=thread, status="archived" if n else "already archived")

    def tool_schedule_followup(self, message_id: str, due: str, note: str = ""):
        m = self._msg(message_id)
        d = parse_date(due)
        if d < NOW.date():
            raise ToolError("due date is in the past")
        fid = self._new("fup")
        self.followups[fid] = dict(id=fid, message_id=message_id, thread_id=m["thread_id"], due=d.isoformat(), note=str(note))
        return dict(followup_id=fid, due=d.isoformat(),
                    status="reminder set; it fires on the due date only if nobody has replied in the thread")

    # ---- Calendar -------------------------------------------------------

    def _event(self, event_id: str) -> dict:
        e = self.events.get(event_id)
        if not e or e["status"] == "cancelled":
            raise ToolError(f"event {event_id!r} not found")
        return e

    def _view(self, e: dict) -> dict:
        mine = "organizer" if e["organizer"] == USER else e["attendees"].get(USER, "not invited")
        return dict(id=e["id"], title=e["title"], start=e["start"], end=e["end"], organizer=self._who(e["organizer"]),
                    attendees={self._who(a): r for a, r in e["attendees"].items()}, my_response=mine,
                    location=e["location"], description=e["description"])

    def tool_list_events(self, start_date: str, end_date: str):
        a, b = parse_date(start_date), parse_date(end_date)
        evs = [e for e in self.events.values()
               if e["status"] == "confirmed" and a <= date.fromisoformat(e["start"][:10]) <= b]
        return dict(events=[self._view(e) for e in sorted(evs, key=lambda e: e["start"])])

    def _span(self, start, end) -> tuple[str, str]:
        s, e = parse_dt(start), parse_dt(end)
        if e <= s:
            raise ToolError("end must be after start")
        return fmt(s), fmt(e)

    def tool_create_event(self, title: str, start: str, end: str, attendees=None, description: str = "", location: str = ""):
        s, e = self._span(start, end)
        att = {a: "needsAction" for a in _addresses(attendees, "attendees") if a != USER}
        eid = self._new("evt")
        self.events[eid] = dict(id=eid, title=str(title), start=s, end=e, organizer=USER, attendees=att,
                                description=str(description), location=str(location), status="confirmed", my_comment="")
        return dict(event_id=eid, start=s, end=e, status="created; invitations sent" if att else "created")

    def _own(self, e: dict, verb: str) -> None:
        if e["organizer"] != USER:
            raise ToolError(f"403 Forbidden: only the organizer ({e['organizer']}) can {verb} this event. "
                            "Use respond_to_event to accept or decline.")

    def tool_update_event(self, event_id: str, title=None, start=None, end=None, attendees=None, description=None, location=None):
        e = self._event(event_id)
        self._own(e, "change")
        s, en = self._span(start or e["start"], end or e["end"])
        e.update(start=s, end=en)
        if title is not None:
            e["title"] = str(title)
        if description is not None:
            e["description"] = str(description)
        if location is not None:
            e["location"] = str(location)
        if attendees is not None:
            new = _addresses(attendees, "attendees")
            e["attendees"] = {a: e["attendees"].get(a, "needsAction") for a in new if a != USER}
        return dict(event=self._view(e), status="updated; attendees notified")

    def tool_cancel_event(self, event_id: str, message: str = ""):
        e = self._event(event_id)
        self._own(e, "cancel")
        e["status"] = "cancelled"
        e["cancel_message"] = str(message)
        return dict(event_id=event_id, status="cancelled; attendees notified")

    def tool_respond_to_event(self, event_id: str, response: str, comment: str = ""):
        e = self._event(event_id)
        if e["organizer"] == USER:
            raise ToolError("you are the organizer of this event; use update_event or cancel_event")
        if response not in {"accepted", "declined", "tentative"}:
            raise ToolError("response must be one of: accepted, declined, tentative")
        e["attendees"][USER] = response
        e["my_comment"] = str(comment)
        return dict(event_id=event_id, my_response=response, status=f"{response}; organizer notified")


def diff(before: dict, after: dict) -> list[dict]:
    """Every user-visible change between two `World.state()` snapshots. Marking mail read is ignored."""
    out: list[dict] = []
    bm, am = before["messages"], after["messages"]
    for mid, m in am.items():
        if mid not in bm and "SENT" in m["labels"]:
            out.append(dict(kind="sent", id=mid, thread_id=m["thread_id"], to=m["to"], cc=m["cc"],
                            subject=m["subject"], body=m["body"]))
    inbox = lambda ms: {m["thread_id"] for m in ms.values() if "INBOX" in m["labels"]}
    for t in sorted(inbox(bm) - inbox(am)):
        out.append(dict(kind="archived", thread_id=t))
    for did, d in after["drafts"].items():
        if did not in before["drafts"]:
            out.append(dict(kind="draft_created", **d))
    for did in before["drafts"]:
        if did not in after["drafts"]:
            out.append(dict(kind="draft_removed", id=did))
    for eid, e in after["events"].items():
        b = before["events"].get(eid)
        if b is None:
            if e["status"] == "confirmed":
                out.append(dict(kind="event_created", **e))
            continue
        if b["status"] == "confirmed" and e["status"] == "cancelled":
            out.append(dict(kind="event_cancelled", id=eid, title=e["title"], start=e["start"],
                            attendees=sorted(e["attendees"]), message=e.get("cancel_message", "")))
            continue
        others = lambda ev: {a: r for a, r in ev["attendees"].items() if a != USER}
        fields = [k for k in ("title", "start", "end", "description", "location") if e[k] != b[k]]
        if others(e) != others(b):
            fields.append("attendees")
        if fields:
            out.append(dict(kind="event_updated", id=eid, fields=fields, title=e["title"], start=e["start"], end=e["end"],
                            attendees=sorted(others(e)), before=dict(title=b["title"], start=b["start"], end=b["end"])))
        if e["attendees"].get(USER) != b["attendees"].get(USER):
            out.append(dict(kind="rsvp", id=eid, title=e["title"], start=e["start"], organizer=e["organizer"],
                            old=b["attendees"].get(USER), new=e["attendees"].get(USER), comment=e.get("my_comment", "")))
    for fid, f in after["followups"].items():
        if fid not in before["followups"]:
            out.append(dict(kind="followup", **f))
    return out
