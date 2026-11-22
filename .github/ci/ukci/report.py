"""Render the plan and verdicts for people: check run, job summary, exit code.

The same markdown goes to the "Catalog Core" check on the PR and to the
workflow run summary, so contributors and reviewers see one story:
what was tested together, at which commits, what broke, whose fault it
is, and how to reproduce it.
"""

from __future__ import annotations

import collections
import glob
import json
import os

from . import verdict as V
from .gh import GitHub

CHECK_NAME = "Catalog Core"
ICON = {V.REGRESSION: "❌", V.FAIL: "❌", V.INFRA: "⚠️", V.FLAKY: "🟡", V.BROKEN_ON_BASE: "⚪", V.PASS: "✅"}
LABEL = {V.REGRESSION: "regression", V.FAIL: "failure", V.INFRA: "CI infrastructure error",
         V.FLAKY: "flaky", V.BROKEN_ON_BASE: "already broken on base", V.PASS: "passed"}
MAX_SUMMARY = 60000      # check run summary limit is 65535


def short(sha: str | None) -> str:
    return (sha or "")[:10]


def title(plan: dict, verdicts: list[V.Verdict]) -> str:
    if plan.get("errors"):
        return f"❌ Not tested: {len(plan['errors'])} problem(s) with the PR set"
    if not plan.get("cells"):
        return "✅ No app uses the changed repositories"
    count = collections.Counter(v.verdict for v in verdicts)
    parts = [f"{count[k]} {LABEL[k]}" for k in V.ORDER if count[k] and k != V.PASS]
    head = ICON[next((k for k in V.ORDER if count[k]), V.PASS)]
    return f"{head} " + (" · ".join(parts + [f"{count[V.PASS]} passed"]) if parts else f"all {count[V.PASS]} passed")


def tested_together(plan: dict) -> list[str]:
    lines = []
    if plan.get("prs"):
        lines += ["### Tested together", "", "| PR | commit | why |", "|---|---|---|"]
        for p in plan["prs"]:
            note = p["via"] if p["used"] else f"{p['via']}; superseded by a PR stacked on top"
            lines.append(f"| [{p['pr']}]({p['url']}) {p['title'][:60]} | `{short(p['merge_sha'])}` (merged into `{p['base_ref']}`) | {note} |")
        for d in plan.get("dropped", []):
            lines.append(f"| {d['pr']} | – | not used: {d['reason']} |")
        lines.append("")
    if plan.get("lock"):
        lines += ["<details><summary>Exact commits</summary>", "", "| repo | commit | from |", "|---|---|---|"]
        cat = plan.get("catalog") or {}
        if cat:
            lines.append(f"| catalog-core | `{short(cat.get('sha'))}` | {cat.get('from')} |")
        for path, e in plan["lock"].items():
            lines.append(f"| {path} ({e['repo']}) | `{short(e['sha'])}` | {e['from']} |")
        lines += ["", "</details>", ""]
    return lines


def render(plan: dict, verdicts: list[V.Verdict] | None, *, running: bool = False) -> str:
    lines: list[str] = []
    if plan.get("errors"):
        lines += ["### Problems (nothing was run)", ""] + [f"- {e}" for e in plan["errors"]] + [""]
    if plan.get("untrusted"):
        lines += ["### Waiting for maintainer approval", "",
                  "These PRs are from authors who are not members of the organization, so a maintainer must "
                  f"approve the run before any of their code is executed: [approve here]({plan.get('run_url')}).", ""]
        lines += [f"- {u}" for u in plan["untrusted"]] + [""]
    lines += tested_together(plan)

    if running:
        lines += ["### Running", ""] + [f"- **{c['app']}** ({c['compiler']}): {c['why']}" for c in plan.get("cells", [])]
    elif verdicts is not None:
        bad = [v for v in verdicts if v.verdict != V.PASS]
        if bad:
            lines += ["### Results", "", "| | app | compiler | variant | what happened |", "|---|---|---|---|---|"]
            for v in sorted(bad, key=lambda v: (V.ORDER.index(v.verdict), v.app, v.compiler, v.variant)):
                lines.append(f"| {ICON[v.verdict]} {LABEL[v.verdict]} | {v.app} | {v.compiler} | {v.variant} | {v.detail} |")
            lines.append("")
        passed = collections.defaultdict(list)
        for v in verdicts:
            if v.verdict == V.PASS:
                passed[(v.app, v.compiler)].append(v.variant)
        if passed:
            lines += ["<details><summary>Passed</summary>", ""]
            lines += [f"- {a} ({c}): {', '.join(vs)}" for (a, c), vs in sorted(passed.items())]
            lines += ["", "</details>", ""]
        lines += ["Only **regressions** (fails with these PRs, passes without them), failures on scheduled runs "
                  "and CI errors block. Flaky and already-broken variants are shown but don't block.", ""]
    if plan.get("cells"):
        why = sorted({(c["app"], c["why"]) for c in plan["cells"]})
        lines += ["<details><summary>Why these apps</summary>", ""] + [f"- **{a}**: {w}" for a, w in why]
        if plan.get("untested_apps"):
            lines.append(f"- not tested (no `.scripts/test/all.sh`): {', '.join(plan['untested_apps'])}")
        lines += ["", "</details>", ""]
    lines += [f"[Workflow run]({plan.get('run_url')}) · plan `{short(plan.get('plan_hash'))}`"]
    text = "\n".join(lines)
    return text if len(text) < MAX_SUMMARY else text[:MAX_SUMMARY] + "\n\n…(truncated, see the workflow run)"


def check_target(plan: dict) -> tuple[str, str] | None:
    """Where the Catalog Core check goes: the root PR's head, in a dependency repo."""
    repo, sha = plan.get("root_repo"), plan.get("root_head_sha")
    if not repo or not sha or repo.split("/")[1] == "catalog-core":
        return None       # catalog-core's own PRs show the workflow run itself
    return repo, sha


def post_check(gh: GitHub, plan: dict, *, status: str, title_text: str, summary: str,
               conclusion: str | None = None, check_id: str | None = None) -> str | None:
    target = check_target(plan)
    if not target or not gh.token:
        return None
    repo, sha = target
    body = {"name": CHECK_NAME, "head_sha": sha, "status": status, "details_url": plan.get("run_url"),
            "output": {"title": title_text[:250], "summary": summary}}
    if conclusion:
        body["conclusion"] = conclusion
    if check_id:
        return str(gh.request("PATCH", f"/repos/{repo}/check-runs/{check_id}", body)["id"])
    return str(gh.request("POST", f"/repos/{repo}/check-runs", body)["id"])


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def cmd_start(args) -> int:
    with open(args.plan) as f:
        plan = json.load(f)
    gh = GitHub()
    if plan.get("errors"):
        t, s, st, concl = title(plan, []), render(plan, []), "completed", "failure"
    elif not plan.get("cells"):
        t, s, st, concl = title(plan, []), render(plan, []), "completed", "success"
    else:
        t = "⏳ Waiting for maintainer approval" if not plan["trusted"] else f"⏳ Testing {len(plan['cells'])} app/compiler combinations"
        s, st, concl = render(plan, None, running=True), "in_progress", None
    write_step_summary(f"## {t}\n\n{s}")
    check_id = post_check(gh, plan, status=st, title_text=t, summary=s, conclusion=concl)
    if check_id and os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"check_id={check_id}\n")
    return 0


def load_results(directory: str) -> list[dict]:
    results = []
    for path in sorted(glob.glob(os.path.join(directory, "**", "result.json"), recursive=True)):
        try:
            with open(path) as f:
                r = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        # Results come from jobs that ran PR code: treat as data, keep only known fields.
        if isinstance(r, dict) and isinstance(r.get("app"), str) and isinstance(r.get("attempts"), list):
            results.append(r)
    return results


def cmd_finish(args) -> int:
    with open(args.plan) as f:
        plan = json.load(f)
    verdicts = V.judge(plan, load_results(args.results)) if not plan.get("errors") else []
    concl = V.conclusion(plan, verdicts)
    t, s = title(plan, verdicts), render(plan, verdicts)
    write_step_summary(f"## {t}\n\n{s}")
    with open(args.out, "w") as f:
        json.dump({"plan_hash": plan["plan_hash"], "conclusion": concl, "title": t,
                   "verdicts": [v.__dict__ for v in verdicts]}, f, indent=2)
    if not plan.get("errors") and plan.get("cells"):
        post_check(GitHub(), plan, status="completed", title_text=t, summary=s, conclusion=concl,
                   check_id=args.check_id or None)
    print(t)
    return 0 if concl == "success" else 1
