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
import urllib.parse

from . import verdict as V
from .gh import GitHub

CHECK_NAME = "Catalog Core"
ICON = {V.REGRESSION: "❌", V.FAIL: "❌", V.INFRA: "⚠️", V.FLAKY: "🟡", V.BROKEN_ON_BASE: "⚪", V.PASS: "✅"}
NAME = {V.REGRESSION: ("regression", "regressions"), V.FAIL: ("failure", "failures"),
        V.INFRA: ("CI error", "CI errors"), V.FLAKY: ("flaky", "flaky"),
        V.BROKEN_ON_BASE: ("already broken", "already broken"), V.PASS: ("passed", "passed")}
MAX_SUMMARY = 60000      # check run summary limit is 65535


def short(sha: str | None) -> str:
    return (sha or "")[:10]


def n_configs(plan: dict) -> int:
    return sum(len(c["variants"]) for c in plan.get("cells", []))


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def title(plan: dict, verdicts: list[V.Verdict]) -> str:
    if plan.get("errors"):
        more = f" (+{len(plan['errors']) - 1} more)" if len(plan["errors"]) > 1 else ""
        return f"❌ Not tested: {plan['errors'][0][:180]}{more}"
    if not plan.get("cells"):
        return "✅ No app uses the changed repositories"
    count = collections.Counter(v.verdict for v in verdicts)
    total = len(verdicts)
    if count[V.PASS] == total:
        return f"✅ All {plural(total, 'configuration')} passed"
    parts = []
    for k in V.ORDER[:-1]:
        if count[k]:
            parts.append(f"{count[k]} {NAME[k][0] if count[k] == 1 else NAME[k][1]}")
    sources = {v.inherited_from for v in verdicts if v.verdict == V.REGRESSION}
    if count[V.REGRESSION] and None not in sources:
        parts[0] += f" (inherited from {', '.join(sorted(sources))})"
    head = ICON[next(k for k in V.ORDER if count[k])]
    return f"{head} " + " · ".join(parts + [f"{count[V.PASS]}/{total} passed"])


def describe(configs: list[tuple[str, str, str]]) -> str:
    """Compact list of (app, compiler, variant): a cross product when it is one."""
    apps = sorted({a for a, _, _ in configs})
    ccs = sorted({c for _, c, _ in configs})
    vs = sorted({v for _, _, v in configs})
    if len(configs) == len(apps) * len(ccs) * len(vs):
        return " × ".join(", ".join(x) for x in (apps, ccs, vs))
    by_app = collections.defaultdict(list)
    for a, c, v in sorted(configs):
        by_app[a].append(f"{c} {v}")
    return "; ".join(f"{a}: {', '.join(x)}" for a, x in by_app.items())


def tested_together(plan: dict) -> list[str]:
    lines = []
    if plan.get("prs"):
        tested = {e.get("pr", ""): e for e in plan.get("lock", {}).values()}
        dropped = {d["pr"]: d["reason"] for d in plan.get("dropped", [])}
        lines += ["### Tested together", "", "| PR | tested as | why |", "|---|---|---|"]
        for p in plan["prs"]:
            entry = next((e for label, e in tested.items() if p["pr"] in label.split(" + ")), None)
            if not p["used"]:
                how = f"not used: {dropped.get(p['pr'], 'superseded')}"
            elif entry and " + " in entry["pr"]:
                how = f"`{short(entry['sha'])}`: merged together with the other {entry['repo'].split('/')[1]} PRs into `{p['base_ref']}`"
            else:
                how = f"`{short((entry or {}).get('sha') or p['merge_sha'])}`: merged into `{p['base_ref']}`"
            lines.append(f"| [{p['pr']}]({p['url']}) {p['title'][:60]} | {how} | {p['via']} |")
        for ref, why in dropped.items():
            if not any(p["pr"] == ref for p in plan["prs"]):
                lines.append(f"| {ref} | not used: {why} | |")
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


def results(verdicts: list[V.Verdict]) -> list[str]:
    """One block per distinct failure, worst first, with the actual error."""
    groups: dict[tuple, list] = collections.defaultdict(list)
    for v in verdicts:
        if v.verdict != V.PASS:
            groups[(v.verdict, v.reason, v.detail)].append((v.app, v.compiler, v.variant))
    lines = []
    for (verdict, reason, detail), configs in sorted(groups.items(), key=lambda g: (V.ORDER.index(g[0][0]), -len(g[1]))):
        label = NAME[verdict][0].capitalize()
        lines += [f"**{ICON[verdict]} {label}** in {plural(len(configs), 'configuration')}: {describe(configs)}",
                  "```", reason, "```"]
        if detail:
            lines.append(detail)
        lines.append("")
    passed = [(v.app, v.compiler, v.variant) for v in verdicts if v.verdict == V.PASS]
    if passed:
        lines += [f"<details><summary>✅ Passed: {plural(len(passed), 'configuration')}</summary>", "",
                  describe(passed), "", "</details>", ""]
    return lines


def render(plan: dict, verdicts: list[V.Verdict] | None, *, running: bool = False) -> str:
    lines: list[str] = [f"> {n}" for n in plan.get("notes", [])] + ([""] if plan.get("notes") else [])
    if plan.get("errors"):
        lines += ["### Not tested", ""] + [f"- {e}" for e in plan["errors"]] + [""]
    if plan.get("untrusted"):
        lines += ["### Waiting for maintainer approval", "",
                  "These PRs are from authors who are not members of the organization, so a maintainer must "
                  f"approve the run before any of their code is executed: [approve here]({plan.get('run_url')}).", ""]
        lines += [f"- {u}" for u in plan["untrusted"]] + [""]
    if running and plan.get("cells"):
        lines += [f"### Testing {plural(n_configs(plan), 'configuration')}", ""]
        apps = collections.defaultdict(list)
        for c in plan["cells"]:
            apps[(c["app"], c["why"])].append(c["compiler"])
        lines += [f"- **{a}** ({', '.join(ccs)}): {why}" for (a, why), ccs in sorted(apps.items())] + [""]
    elif verdicts:
        lines += ["### Results", ""] + results(verdicts)
    lines += tested_together(plan)
    if plan.get("cells") and not running:
        why = sorted({(c["app"], c["why"]) for c in plan["cells"]})
        lines += ["<details><summary>Why these apps</summary>", ""] + [f"- **{a}**: {w}" for a, w in why]
        if plan.get("untested_apps") and not plan.get("root"):
            lines.append(f"- not tested (no `.scripts/test/all.sh`): {', '.join(plan['untested_apps'])}")
        lines += ["", "</details>", ""]
    repo = (plan.get("catalog") or {}).get("repo")
    help_link = f" · [what the results mean](https://github.com/{repo}/blob/HEAD/.github/ci/README.md)" if repo else ""
    lines += [f"[Workflow run]({plan.get('run_url')}){help_link} · plan `{short(plan.get('plan_hash'))}`"]
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


def post_member_checks(gh: GitHub, plan: dict, *, status: str, title_text: str, summary: str,
                       conclusion: str | None = None) -> None:
    """Finish the check on the other PRs of a cycle this run tested."""
    if not gh.token:
        return
    for m in plan.get("cycle", []):
        if m["pr"] == plan.get("root"):
            continue
        body = {"name": CHECK_NAME, "head_sha": m["head_sha"], "status": status, "details_url": plan.get("run_url"),
                "output": {"title": title_text[:250], "summary": summary}}
        if conclusion:
            body["conclusion"] = conclusion
        try:
            existing = gh.get(f"/repos/{m['repo']}/commits/{m['head_sha']}/check-runs?"
                              + urllib.parse.urlencode({"check_name": CHECK_NAME}))
            ids = [c["id"] for c in (existing or {}).get("check_runs", [])]
            if ids:
                gh.request("PATCH", f"/repos/{m['repo']}/check-runs/{ids[0]}", body)
            else:
                gh.request("POST", f"/repos/{m['repo']}/check-runs", body)
        except Exception as e:     # e.g. the token has no access to that repo
            print(f"::warning::could not update the check on {m['pr']}: {e}")


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def cmd_start(args) -> int:
    with open(args.plan) as f:
        plan = json.load(f)
    gh = GitHub()
    if plan.get("delegate_to"):
        t, s, st, concl = (f"⏳ Tested together with {plan['delegate_to']}", render(plan, None), "in_progress", None)
    elif plan.get("errors"):
        t, s, st, concl = title(plan, []), render(plan, []), "completed", "failure"
    elif not plan.get("cells"):
        t, s, st, concl = title(plan, []), render(plan, []), "completed", "success"
    else:
        t = ("⏳ Waiting for maintainer approval" if not plan["trusted"]
             else f"⏳ Testing {plural(n_configs(plan), 'configuration')}")
        s, st, concl = render(plan, None, running=True), "in_progress", None
    write_step_summary(f"## {t}\n\n{s}")
    check_id = None
    try:
        check_id = post_check(gh, plan, status=st, title_text=t, summary=s, conclusion=concl)
        if plan.get("cells") and not plan.get("delegate_to"):
            post_member_checks(gh, plan, status=st, title_text=t, summary=s)
    except Exception as e:     # a check is feedback; never lose the run over it
        print(f"::warning::could not post the Catalog Core check: {e}")
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


def blame(plan: dict, verdicts: list[V.Verdict], store) -> None:
    """For each regression, say whether a dependency PR breaks it on its own.

    Every PR in the set has its own run (triggered by its own pushes). If
    that run already fails the same app/compiler/variant, the failure is
    inherited from it, not caused by the PR being tested here.
    """
    deps = [p for p in plan.get("prs", []) if p["used"] and p["pr"] != plan.get("root")]
    if not deps or store is None:
        return
    from .data import own_result
    for v in verdicts:
        if v.verdict != V.REGRESSION:
            continue
        inherited, waiting = [], []
        for d in deps:
            own = own_result(store, d["pr"], d["head_sha"])
            if own is None:
                waiting.append(d["pr"])
                continue
            mine = next((x for x in own.get("verdicts", []) if (x.get("app"), x.get("compiler"), x.get("variant"))
                         == (v.app, v.compiler, v.variant)), None)
            if mine and mine.get("verdict") in (V.REGRESSION, V.FAIL):
                inherited.append(f"[{d['pr']}]({own.get('run_url')})")
        if inherited:
            v.inherited_from = ", ".join(i.split("]")[0].lstrip("[") for i in inherited)
            v.detail += f". **Inherited**: it also fails in the own run of {', '.join(inherited)}"
        elif waiting:
            v.detail += f". Not known yet whether it comes from {', '.join(waiting)} (no own run yet)"
        else:
            v.detail += ". The dependencies pass on their own, so it comes from this PR (or the combination)"


def nightly_changes(previous: dict | None, verdicts: list[V.Verdict]) -> tuple[list[str], list[str]]:
    """(newly failing, recovered) cells compared to the previous scheduled run."""
    bad = {V.FAIL, V.INFRA, V.REGRESSION}
    prev = {k: v for k, v in ((previous or {}).get("cells") or {}).items()}
    broke, fixed = [], []
    for v in verdicts:
        k = f"{v.app} {v.compiler} {v.variant}"
        was_bad, is_bad = prev.get(k) in bad, v.verdict in bad
        if is_bad and not was_bad and k in prev:
            broke.append(f"{k}: {v.detail}")
        elif was_bad and not is_bad:
            fixed.append(k)
    return broke, fixed


def discord(webhook: str, text: str, mention: str = "") -> None:
    import urllib.request
    body = {"content": (mention + "\n" if mention else "") + text[:1900],
            # Only the configured mention may ping; nothing from test output can.
            "allowed_mentions": {"parse": ["roles", "users"] if mention else []}}
    req = urllib.request.Request(webhook, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "catalog-core-ci"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:      # never fail the run because of a notification
        print(f"::warning::Discord notification failed: {e}")


def notify_nightly(plan: dict, verdicts: list[V.Verdict], t: str, store) -> None:
    rel = f"nightly/{plan.get('channel', 'staging')}/latest.json"
    previous = store.read(rel) if store else None
    broke, fixed = nightly_changes(previous, verdicts)
    if store:
        store.write(rel, {"run_url": plan.get("run_url"),
                          "cells": {f"{v.app} {v.compiler} {v.variant}": v.verdict for v in verdicts}})
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    if previous is None:
        discord(webhook, f"**catalog-core nightly ({plan.get('channel')})**: first recorded run. {t}\n{plan.get('run_url')}")
    elif broke or fixed:
        lines = [f"**catalog-core nightly ({plan.get('channel')})**: {t}"]
        lines += ["**Newly failing**"] + [f"• {b}" for b in broke[:15]] if broke else []
        lines += ["**Fixed**"] + [f"• {f}" for f in fixed[:15]] if fixed else []
        lines.append(plan.get("run_url", ""))
        discord(webhook, "\n".join(lines), os.environ.get("DISCORD_MENTION", "") if broke else "")
    else:
        print("nightly: nothing changed since the previous run; no Discord message")


def open_store(write: bool):
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return None
    from .data import Store
    try:
        return Store(repo, os.environ.get("CI_DATA_TOKEN") if write else None).open()
    except Exception as e:
        print(f"::warning::ci-data unavailable: {e}")
        return None


def declined(plan: dict) -> dict | None:
    """The maintainer review that declined to run this PR set, if any."""
    repo, run_id = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_RUN_ID")
    if plan.get("trusted") or not repo or not run_id:
        return None
    try:
        reviews = GitHub(os.environ.get("CI_DATA_TOKEN")).get(f"/repos/{repo}/actions/runs/{run_id}/approvals") or []
    except Exception:
        return None
    return next((r for r in reviews if r.get("state") == "rejected"), None)


def cmd_finish(args) -> int:
    with open(args.plan) as f:
        plan = json.load(f)
    results = load_results(args.results)
    if os.environ.get("TEST_RESULT") == "cancelled":
        # Superseded by a newer push (or cancelled by hand): nothing to judge or record.
        t = "⏹ Cancelled: a newer run replaced this one, or it was cancelled"
        write_step_summary(f"## {t}")
        try:
            gh = GitHub()
            post_check(gh, plan, status="completed", title_text=t, summary=render(plan, None),
                       conclusion="cancelled", check_id=args.check_id or None)
            post_member_checks(gh, plan, status="completed", title_text=t, summary=render(plan, None),
                               conclusion="cancelled")
        except Exception as e:
            print(f"::warning::could not finish the Catalog Core check: {e}")
        print(t)
        return 0
    if not results and (review := declined(plan)):
        who = (review.get("user") or {}).get("login", "a maintainer")
        t = f"⛔ Not run: @{who} declined to run this code"
        s = (f"A maintainer declined to run the code of these PRs"
             + (f": \"{review['comment']}\"" if review.get("comment") else "") + "\n\n" + render(plan, None))
        write_step_summary(f"## {t}\n\n{s}")
        try:
            gh = GitHub()
            post_check(gh, plan, status="completed", title_text=t, summary=s, conclusion="action_required",
                       check_id=args.check_id or None)
            post_member_checks(gh, plan, status="completed", title_text=t, summary=s, conclusion="action_required")
        except Exception as e:
            print(f"::warning::could not finish the Catalog Core check: {e}")
        print(t)
        return 1
    verdicts = V.judge(plan, results) if not plan.get("errors") else []
    store = open_store(write=True)
    blame(plan, verdicts, store)
    concl = V.conclusion(plan, verdicts)
    t, s = title(plan, verdicts), render(plan, verdicts)
    write_step_summary(f"## {t}\n\n{s}")
    report = {"plan_hash": plan["plan_hash"], "conclusion": concl, "title": t,
              "verdicts": [v.__dict__ for v in verdicts]}
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)
    if not plan.get("errors") and plan.get("cells"):
        gh = GitHub()
        try:
            post_check(gh, plan, status="completed", title_text=t, summary=s, conclusion=concl,
                       check_id=args.check_id or None)
            post_member_checks(gh, plan, status="completed", title_text=t, summary=s, conclusion=concl)
        except Exception as e:
            print(f"::warning::could not finish the Catalog Core check: {e}")
    if plan.get("event") == "schedule":
        notify_nightly(plan, verdicts, t, store)
    if store and os.environ.get("CI_DATA_TOKEN"):
        from .data import record_run
        record_run(store, plan, report, results)
        store.commit_and_push(f"{plan.get('event')}: {plan.get('root') or plan.get('channel')}: {concl}")
    print(t)
    return 0 if concl == "success" else 1


def cmd_fan_out(args) -> int:
    """Re-test PRs that were tested together with this one (they depend on it)."""
    with open(args.plan) as f:
        plan = json.load(f)
    if not plan.get("fan_out") or not plan.get("root") or plan.get("errors"):
        return 0
    gh = GitHub()

    def dispatch(ref: str, cascade: str) -> None:
        repo, _, n = ref.partition("#")
        gh.request("POST", f"/repos/{os.environ['GITHUB_REPOSITORY']}/dispatches",
                   {"event_type": "catalog_pr", "client_payload": {"repo": repo, "number": n, "cascade": cascade}})

    if plan.get("delegate_to"):
        print(f"{plan['root']} changed: running the shared test for {plan['delegate_to']}")
        dispatch(plan["delegate_to"], "true")
        return 0
    store = open_store(write=False)
    if store is None:
        return 0
    from .data import dependents
    in_cycle = {m["pr"] for m in plan.get("cycle", [])}
    for ref in dependents(store, plan["root"]):
        if ref not in in_cycle:
            print(f"{plan['root']} changed: re-testing {ref}, which was tested together with it")
            dispatch(ref, "false")
    return 0
