"""Run the scenarios against Claude models, grade the final state, and check the agent's claims.

    python run.py                                  # 25 scenarios x 3 models x 3 trials, Messages API
    python run.py --backend cli                    # same, through `claude -p` on your Claude login
    python run.py --models haiku-4.5 --trials 1 --scenarios decline_the_3pm
    python run.py --regrade                        # re-grade saved runs offline (no model calls)

Each run is saved to results/runs/<model>/<scenario>__<trial>.json; finished runs are skipped on re-run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import anthropic

from harness.agent import MODELS, run_agent
from harness.claims import extract, extract_cli, unreported, verify
from harness.cli_agent import run_agent_cli
from harness.scenarios import SCENARIOS, grade

OUT = Path("results/runs")


def replay(sc, tool_calls):
    """Rebuild the final state by replaying the logged tool calls on a fresh world (faults fire the same way)."""
    w = sc.world()
    before = w.state()
    for c in tool_calls:
        w.call(c["tool"], c["args"])
    return w, before


def score(sc, rec: dict) -> dict:
    w, before = replay(sc, rec["tool_calls"])
    g = grade(sc, w, before)
    threads = {k: w.messages[v]["thread_id"] for k, v in w.ids.items() if v in w.messages}
    ex = rec["claims"]["extracted"]
    rec["grade"] = g
    rec["claims"]["verified"] = verify(ex["claims"], g["mutations"], sc, w.ids, threads)
    rec["claims"]["unreported"] = unreported(ex["claims"], g["mutations"])
    return rec


async def one(client, backend, sem, model, sc, trial, path: Path):
    async with sem:
        people = sc.seed().people
        try:
            if backend == "cli":
                res = await run_agent_cli(model, sc.id, sc.instruction)
                ex = await extract_cli(sc, people, res["final_text"])
            else:
                res = await run_agent(client, model, sc.instruction, sc.world())
                ex = await extract(client, sc, people, res["final_text"])
        except (anthropic.APIError, RuntimeError) as e:
            print(f"  ! {model} {sc.id} #{trial}: {type(e).__name__}: {e}", file=sys.stderr)
            return
        rec = dict(model=model, scenario=sc.id, category=sc.category, trial=trial, instruction=sc.instruction,
                   **res, claims=dict(extracted=ex))
        rec = score(sc, rec)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rec, indent=1))
        false = [c["quote"] for c in rec["claims"]["verified"] if not c["supported"]]
        print(f"  {model:10} {sc.id:32} #{trial}  {'PASS' if rec['grade']['passed'] else 'fail'}"
              f"{'  FALSE CLAIM: ' + repr(false) if false else ''}  ${rec['cost_usd']:.3f}", flush=True)


def load_env(path: Path = Path(".env")) -> None:
    """Pick up ANTHROPIC_API_KEY from a gitignored .env so it never has to be typed into a shell."""
    if path.exists():
        for line in path.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--scenarios", default="all")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--backend", choices=["api", "cli"], default="api")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--regrade", action="store_true", help="re-grade saved runs offline")
    ap.add_argument("--reextract", action="store_true", help="redo claim extraction on saved runs, then re-grade")
    a = ap.parse_args()

    if a.regrade:
        for p in sorted(OUT.glob("*/*.json")):
            rec = json.loads(p.read_text())
            p.write_text(json.dumps(score(SCENARIOS[rec["scenario"]], rec), indent=1))
        print("regraded", len(list(OUT.glob("*/*.json"))), "runs")
        return

    client = None
    if a.backend == "api":
        load_env()
        client = anthropic.AsyncAnthropic(max_retries=8)
    sem = asyncio.Semaphore(a.concurrency)

    if a.reextract:
        async def redo(p: Path):
            async with sem:
                rec = json.loads(p.read_text())
                sc = SCENARIOS[rec["scenario"]]
                ex = (await extract_cli(sc, sc.seed().people, rec["final_text"]) if a.backend == "cli"
                      else await extract(client, sc, sc.seed().people, rec["final_text"]))
                rec["claims"] = dict(extracted=ex)
                p.write_text(json.dumps(score(sc, rec), indent=1))
        paths = sorted(OUT.glob("*/*.json"))
        for r in await asyncio.gather(*map(redo, paths), return_exceptions=True):
            if isinstance(r, BaseException):
                print(f"  ! {type(r).__name__}: {r}", file=sys.stderr)
        print("re-extracted", len(paths), "runs")
        return

    scs = list(SCENARIOS.values()) if a.scenarios == "all" else [SCENARIOS[s] for s in a.scenarios.split(",")]
    jobs = []
    for model in a.models.split(","):
        for sc in scs:
            for t in range(1, a.trials + 1):
                path = OUT / model / f"{sc.id}__{t}.json"
                if not path.exists():
                    jobs.append(one(client, a.backend, sem, model, sc, t, path))
    print(f"{len(jobs)} runs to do ({a.backend} backend)", flush=True)
    for r in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(r, BaseException):
            print(f"  ! {type(r).__name__}: {r}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
