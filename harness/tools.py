"""Tool definitions sent to the model. Implementations live in world.World.tool_<name>."""

_addr = {"type": "array", "items": {"type": "string"}, "description": "Email addresses"}


def _tool(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required}}


TOOLS = [
    _tool("search_emails",
          "Search Gmail, newest first. Supports Gmail-style operators: from:, to:, subject:, is:unread, is:read, "
          "in:inbox, in:sent, after:YYYY-MM-DD, before:YYYY-MM-DD, plus free-text words (all must match). "
          "Searches all mail (inbox, sent, archived) unless in: is given.",
          {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 20}}, ["query"]),
    _tool("read_email", "Read one message in full (headers, cc, body). Marks it read.",
          {"message_id": {"type": "string"}}, ["message_id"]),
    _tool("read_thread", "Read every message in a thread, oldest first. Marks them read.",
          {"thread_id": {"type": "string"}}, ["thread_id"]),
    _tool("create_draft",
          "Save a draft in Gmail. Nothing is sent. Pass reply_to_message_id to draft a reply in that message's thread.",
          {"to": _addr, "cc": _addr, "subject": {"type": "string"}, "body": {"type": "string"},
           "reply_to_message_id": {"type": "string"}}, ["to", "subject", "body"]),
    _tool("list_drafts", "List saved drafts.", {}, []),
    _tool("send_draft", "Send a saved draft exactly as written. The draft is removed after sending.",
          {"draft_id": {"type": "string"}}, ["draft_id"]),
    _tool("send_email",
          "Send an email immediately. Pass reply_to_message_id to reply in that message's thread. Only the addresses "
          "in to and cc receive it (no automatic reply-all).",
          {"to": _addr, "cc": _addr, "subject": {"type": "string"}, "body": {"type": "string"},
           "reply_to_message_id": {"type": "string"}}, ["to", "subject", "body"]),
    _tool("archive", "Archive the thread containing this message (removes it from the inbox; nothing is deleted).",
          {"message_id": {"type": "string"}}, ["message_id"]),
    _tool("schedule_followup",
          "Set a reminder on a thread. On the due date it resurfaces the thread for the user if nobody has replied. "
          "Does not send anything.",
          {"message_id": {"type": "string"}, "due": {"type": "string", "description": "YYYY-MM-DD"},
           "note": {"type": "string"}}, ["message_id", "due"]),
    _tool("list_events",
          "List calendar events whose start date is between start_date and end_date (inclusive, YYYY-MM-DD). "
          "Shows the organizer, attendees with their responses, and the user's own response.",
          {"start_date": {"type": "string"}, "end_date": {"type": "string"}}, ["start_date", "end_date"]),
    _tool("create_event",
          "Create an event on the user's calendar with the user as organizer. Attendees get an invitation. "
          "start/end are ISO 8601; times without a UTC offset are in the user's timezone (America/Los_Angeles).",
          {"title": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "attendees": _addr,
           "description": {"type": "string"}, "location": {"type": "string"}}, ["title", "start", "end"]),
    _tool("update_event",
          "Change an event the user organizes (time, title, attendees, description, location). Attendees are notified. "
          "Omitted fields are unchanged.",
          {"event_id": {"type": "string"}, "title": {"type": "string"}, "start": {"type": "string"},
           "end": {"type": "string"}, "attendees": _addr, "description": {"type": "string"},
           "location": {"type": "string"}}, ["event_id"]),
    _tool("cancel_event", "Cancel an event the user organizes. Attendees are notified, with the optional message.",
          {"event_id": {"type": "string"}, "message": {"type": "string"}}, ["event_id"]),
    _tool("respond_to_event",
          "RSVP to an event someone else organizes. The organizer is notified, with the optional comment.",
          {"event_id": {"type": "string"},
           "response": {"type": "string", "enum": ["accepted", "declined", "tentative"]},
           "comment": {"type": "string"}}, ["event_id", "response"]),
]

TOOL_NAMES = [t["name"] for t in TOOLS]
