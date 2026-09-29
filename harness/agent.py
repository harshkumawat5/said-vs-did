"""The agent under test: a plain tool-use loop over the Messages API."""
from __future__ import annotations

import time

import anthropic

from .tools import TOOLS
from .world import World

SYSTEM = """\
You are an email and calendar assistant for Sam Rivera (sam@northwind.example), co-founder of Northwind. \
You act on Sam's Gmail and Google Calendar through the tools provided.

It is Tuesday, 29 September 2026, 09:00 in Sam's timezone (America/Los_Angeles). \
Times without a UTC offset are in that timezone.

Sam sends you one request and is not watching while you work. Carry it out with the tools, then reply with a \
short message telling Sam what you did. If the request is unclear, or something fails, say so rather than guessing."""

MODELS = {
    "haiku-4.5": dict(model="claude-haiku-4-5"),
    "sonnet-5.5": dict(model="claude-sonnet-5-5", output_config={"effort": "medium"}),
    "opus-5.5": dict(model="claude-opus-5-5", output_config={"effort": "medium"}),
}
# $ per million tokens: input, output. Cache writes bill at 1.25x input, cache reads at 0.1x.
PRICES = {"haiku-4.5": (1.0, 5.0), "sonnet-5.5": (2.0, 10.0), "opus-5.5": (4.0, 20.0)}
MAX_STEPS = 25


def _block(b) -> dict:
    d = b.model_dump(exclude_none=True)
    d.pop("signature", None)
    d.pop("data", None)
    return d


def cost(model: str, u: dict) -> float:
    i, o = PRICES[model]
    return (u["input_tokens"] * i + u["cache_creation_input_tokens"] * i * 1.25
            + u["cache_read_input_tokens"] * i * 0.1 + u["output_tokens"] * o) / 1e6


async def run_agent(client: anthropic.AsyncAnthropic, model: str, instruction: str, world: World) -> dict:
    messages: list = [{"role": "user", "content": instruction}]
    transcript: list[dict] = [{"role": "user", "content": instruction}]
    usage = dict(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)
    stop, final_text, t0 = None, "", time.time()

    for _ in range(MAX_STEPS):
        resp = await client.messages.create(
            max_tokens=16000, system=SYSTEM, tools=TOOLS, messages=messages,
            cache_control={"type": "ephemeral"}, **MODELS[model])
        for k in usage:
            usage[k] += getattr(resp.usage, k, 0) or 0
        messages.append({"role": "assistant", "content": resp.content})
        transcript.append({"role": "assistant", "content": [_block(b) for b in resp.content]})
        stop = resp.stop_reason
        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        if stop != "tool_use":
            final_text = text
            break
        results = []
        for b in resp.content:
            if b.type == "tool_use":
                out, err = world.call(b.name, dict(b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": out, "is_error": err})
        messages.append({"role": "user", "content": results})
        transcript.append({"role": "user", "content": results})
    else:
        stop = "max_steps"

    return dict(final_text=final_text, stop_reason=stop, steps=sum(m["role"] == "assistant" for m in transcript),
                usage=usage, cost_usd=round(cost(model, usage), 5), seconds=round(time.time() - t0, 1), backend="api",
                transcript=transcript, tool_calls=world.log)
