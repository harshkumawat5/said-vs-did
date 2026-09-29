"""Summarise results/runs into results/summary.md and results/false_claims.md."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from harness.agent import MODELS
from harness.scenarios import SCENARIOS

RUNS = Path("results/runs")


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}%" if b else "–"


def main() -> None:
    runs = [json.loads(p.read_text()) for p in sorted(RUNS.glob("*/*.json"))]
    models = [m for m in MODELS if any(r["model"] == m for r in runs)]
    by = defaultdict(list)
    for r in runs:
        by[r["model"]].append(r)
        by[(r["model"], r["scenario"])].append(r)
        by[(r["model"], r["category"])].append(r)

    false = lambda r: [c for c in r["claims"]["verified"] if not c["supported"]]
    side = lambda r: not next(c for c in r["grade"]["checks"] if c["name"] == "no unrequested side effects")["ok"]
    lines = ["| | " + " | ".join(models) + " |", "|---|" + "---:|" * len(models)]

    def row(label, f):
        lines.append(f"| {label} | " + " | ".join(f(by[m]) for m in models) + " |")

    row("runs", lambda rs: str(len(rs)))
    row("**task success** (final state correct)", lambda rs: f"**{pct(sum(r['grade']['passed'] for r in rs), len(rs))}**")

    def pass_k(m):
        scs = {r["scenario"] for r in by[m]}
        return sum(all(r["grade"]["passed"] for r in by[(m, s)]) for s in scs), len(scs)

    lines.append("| scenarios passed on every trial (pass^k) | " +
                 " | ".join("{}/{}".format(*pass_k(m)) for m in models) + " |")
    row("**runs with a false claim**", lambda rs: f"**{pct(sum(bool(false(r)) for r in rs), len(rs))}** "
                                                  f"({sum(bool(false(r)) for r in rs)})")
    row("false claims / all action claims", lambda rs: f"{sum(len(false(r)) for r in rs)}/"
                                                       f"{sum(len(r['claims']['verified']) for r in rs)}")
    row("runs with an unrequested side effect", lambda rs: pct(sum(side(r) for r in rs), len(rs)))
    row("runs with an unmentioned action", lambda rs: pct(sum(bool(r["claims"]["unreported"]) for r in rs), len(rs)))
    row("runs that ended by asking Sam something",
        lambda rs: pct(sum(r["claims"]["extracted"]["asked_question"] for r in rs), len(rs)))
    row("mean model calls per run", lambda rs: f"{sum(r['steps'] for r in rs) / len(rs):.1f}")
    row("mean cost per run", lambda rs: f"${sum(r['cost_usd'] for r in rs) / len(rs):.3f}")
    summary = lines

    cats = sorted({s.category for s in SCENARIOS.values()}, key=lambda c: [s.category for s in SCENARIOS.values()].index(c))
    cat = ["| category (scenarios) | " + " | ".join(models) + " |", "|---|" + "---:|" * len(models)]
    for c in cats:
        n = sum(s.category == c for s in SCENARIOS.values())
        cat.append(f"| {c} ({n}) | " + " | ".join(
            pct(sum(r["grade"]["passed"] for r in by[(m, c)]), len(by[(m, c)])) for m in models) + " |")

    per = ["| scenario | " + " | ".join(models) + " |", "|---|" + ":---:|" * len(models)]
    for sid, sc in SCENARIOS.items():
        cells = []
        for m in models:
            rs = by[(m, sid)]
            if not rs:
                cells.append("")
                continue
            f = sum(bool(false(r)) for r in rs)
            cells.append(f"{sum(r['grade']['passed'] for r in rs)}/{len(rs)}" + (f" ⚠{f}" if f else ""))
        per.append(f"| `{sid}` | " + " | ".join(cells) + " |")

    # Sensitivity: the same claims checked against a world where every write was silently dropped.
    from harness.claims import verify
    from run import replay
    with_claims = [r for r in runs if r["claims"]["extracted"]["claims"]]
    caught = 0
    for r in with_claims:
        sc = SCENARIOS[r["scenario"]]
        w, _ = replay(sc, [])
        threads = {k: w.messages[v]["thread_id"] for k, v in w.ids.items() if v in w.messages}
        caught += any(not c["supported"] for c in verify(r["claims"]["extracted"]["claims"], [], sc, w.ids, threads))
    summary += ["", f"Checker sensitivity: replaying the same final messages against a world where every write was "
                    f"silently dropped flags {caught}/{len(with_claims)} runs that made a claim."]

    md = ["## Overall", "", *summary, "", "## By category", "", *cat, "",
          "## By scenario", "", "`k/n` = trials whose final state passed. `⚠n` = trials whose final message claimed "
          "something the state doesn't show.", "", *per, ""]
    Path("results/summary.md").write_text("\n".join(md))

    fc = ["# Every false claim", "",
          "Each entry: what the agent told Sam, what the checker found, and the tool errors the agent saw.", ""]
    for r in runs:
        for c in false(r):
            errs = [f"{t['tool']}: {t['result'][:140]}" for t in r["tool_calls"] if t["is_error"]]
            fc += [f"### {r['model']} · `{r['scenario']}` · trial {r['trial']}", "",
                   f"- **Claim:** \"{c['quote']}\" ({c['action']})", f"- **Checker:** {c['why']}",
                   f"- **Tool errors seen:** {'; '.join(errs) if errs else 'none'}",
                   f"- **Task passed:** {r['grade']['passed']}", "", "<details><summary>final message</summary>", "",
                   "```", r["final_text"], "```", "</details>", ""]
    if not any(false(r) for r in runs):
        fc.append(f"None. All {sum(len(r['claims']['verified']) for r in runs)} completed-action claims in "
                  f"{len(runs)} runs match the final state. See the README for the 4 flags from the first runs, "
                  "which were checker errors, and how they were fixed.")
    Path("results/false_claims.md").write_text("\n".join(fc))
    print("\n".join(md))


if __name__ == "__main__":
    main()
