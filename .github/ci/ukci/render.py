"""What people read: the check title and summary, and the dashboard page."""

from __future__ import annotations

import collections

import jinja2

from .models import Plan, Verdict, VerdictName
from .verdict import ORDER

ICON = {"REGRESSION": "❌", "FAIL": "❌", "INFRA": "⚠️", "FLAKY": "🟡", "BROKEN_ON_BASE": "⚪", "PASS": "✅"}
NAME = {
    "REGRESSION": ("regression", "regressions"),
    "FAIL": ("failure", "failures"),
    "INFRA": ("CI error", "CI errors"),
    "FLAKY": ("flaky", "flaky"),
    "BROKEN_ON_BASE": ("already broken", "already broken"),
    "PASS": ("passed", "passed"),
}
MAX_SUMMARY = 60000  # GitHub's limit for a check summary is 65535
README = ".github/ci/README.md"


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def oneline(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def cell(text: str) -> str:
    """Safe inside a markdown table cell."""
    return oneline(text, 300).replace("|", "\\|")


def code(text: str) -> str:
    return f"`{cell(text).replace('`', chr(39))}`" if text else ""


def title(plan: Plan, verdicts: list[Verdict]) -> str:
    if plan.errors:
        more = f" (+{len(plan.errors) - 1} more)" if len(plan.errors) > 1 else ""
        return f"❌ Not tested: {oneline(plan.errors[0], 180)}{more}"
    if not plan.cells:
        return "✅ No app uses the changed repositories"
    count = collections.Counter(v.verdict for v in verdicts)
    if count["PASS"] == len(verdicts):
        return f"✅ All {plural(len(verdicts), 'configuration')} passed"
    parts = [f"{count[k]} {NAME[k][count[k] != 1]}" for k in ORDER[:-1] if count[k]]
    worst = next(k for k in ORDER if count[k])
    return f"{ICON[worst]} " + " · ".join([*parts, f"{count['PASS']}/{len(verdicts)} passed"])


def describe(configs: list[tuple[str, str, str]], logs: dict[tuple[str, str], str]) -> str:
    """'nginx gcc: qemu.x86_64, fc.x86_64; redis clang: …', each job linked to its log."""
    by_job: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for app, cc, variant in sorted(configs):
        by_job[app, cc].append(variant)
    out = []
    for (app, cc), variants in by_job.items():
        name = f"{app} {cc}"
        out.append(f"{f'[{name}]({logs[app, cc]})' if (app, cc) in logs else name}: {', '.join(variants)}")
    return "; ".join(out)


def _results(verdicts: list[Verdict], logs: dict[tuple[str, str], str]) -> list[str]:
    groups: dict[tuple[VerdictName, str], list[tuple[str, str, str]]] = collections.defaultdict(list)
    for v in verdicts:
        if v.verdict != "PASS":
            groups[v.verdict, v.reason].append((v.app, v.compiler, v.variant))
    rows = sorted(groups.items(), key=lambda g: (ORDER.index(g[0][0]), -len(g[1])))
    lines = []
    if rows:
        lines += ["| verdict | configurations | first error |", "|---|---|---|"]
        lines += [f"| {ICON[k]} {NAME[k][0]} | {describe(c, logs)} | {code(reason)} |" for (k, reason), c in rows]
        lines.append("")
    passed = [(v.app, v.compiler, v.variant) for v in verdicts if v.verdict == "PASS"]
    if passed:
        lines += [f"<details><summary>✅ {plural(len(passed), 'configuration')} passed</summary>", ""]
        lines += [describe(passed, logs), "", "</details>", ""]
    return lines


def _tested_as(plan: Plan) -> dict[str, str]:
    """PR -> how its code went into the test."""
    by_pr = {ref: e for e in plan.lock.values() if e.pr for ref in e.pr.split(" + ")}
    out = {}
    for pr in plan.prs:
        e = by_pr.get(pr.ref)
        if not pr.used:
            out[pr.ref] = f"not used: {pr.note}"
        elif e is None:
            out[pr.ref] = "not needed by the tested apps"
        elif e.parts:
            others = ", ".join(x.label for x in e.parts if x.label != pr.ref)
            out[pr.ref] = f"`{e.sha[:10]}`, merged with {others} into `{pr.base_ref}`"
        else:
            out[pr.ref] = f"`{e.sha[:10]}`, merged into `{pr.base_ref}`"
    return out


def check(
    plan: Plan,
    verdicts: list[Verdict] | None = None,
    *,
    running: bool = False,
    banner: str = "",
    logs: dict[tuple[str, str], str] | None = None,
) -> str:
    """The check summary (also the workflow run summary)."""
    lines = [f"> {cell(x)}" for x in ([banner] if banner else []) + plan.notes]
    if lines:
        lines.append("")
    if plan.errors:
        lines += ["**Not tested**", ""] + [f"- {oneline(e, 1000)}" for e in plan.errors] + [""]
    if running and not plan.trusted:
        lines += [
            f"**Waiting for a maintainer to [approve the run]({plan.run_url})**: these PRs are by authors "
            "who aren't members, and none of their code runs before that.",
            "",
            *[f"- {cell(u)}" for u in plan.untrusted],
            "",
        ]
    if running and plan.cells:
        by_app: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
        for c in plan.cells:
            by_app[c.app, c.why].append(c.compiler)
        lines += [f"**Testing {plural(plan.n_configs, 'configuration')}**", ""]
        lines += [f"- {app} ({', '.join(ccs)}): {why}" for (app, why), ccs in sorted(by_app.items())] + [""]
    if verdicts is not None:
        lines += _results(verdicts, logs or {})
    if plan.prs:
        tested_as = _tested_as(plan)
        lines += ["| PR | tested as | found via |", "|---|---|---|"]
        lines += [
            f"| [{p.ref}]({p.url}) {cell(p.title[:60])} | {cell(tested_as[p.ref])} | {cell(p.via)} |" for p in plan.prs
        ]
        lines.append("")
    if plan.lock:
        lines += ["<details><summary>Exact commits</summary>", "", "| repo | commit | from |", "|---|---|---|"]
        if plan.catalog:
            lines.append(f"| catalog-core | `{plan.catalog.sha[:10]}` | {cell(plan.catalog.source)} |")
        lines += [f"| {path} ({e.repo}) | `{e.sha[:10]}` | {cell(e.source)} |" for path, e in sorted(plan.lock.items())]
        lines += ["", "</details>", ""]
    footer = [f"[Workflow run]({plan.run_url})"]
    if plan.catalog:
        footer.append(f"[how to read this](https://github.com/{plan.catalog.repo}/blob/{plan.catalog.sha}/{README})")
    footer.append(f"plan `{plan.plan_hash[:10]}`")
    text = "\n".join([*lines, " · ".join(footer)])
    return text if len(text) < MAX_SUMMARY else text[:MAX_SUMMARY] + "\n\n…(truncated, see the workflow run)"


def reused(plan: Plan) -> str:
    assert plan.reused
    note = (
        f"> These exact commits were already tested in [this run]({plan.reused.run_url}); its result is "
        "shown below. Comment `recheck` to run the tests again.\n\n"
    )
    return note + plan.reused.summary


_html = jinja2.Environment(
    loader=jinja2.PackageLoader("ukci", "templates"), autoescape=True, undefined=jinja2.StrictUndefined
)


def dashboard(**context) -> str:
    return _html.get_template("dashboard.html.j2").render(icon=ICON, name=NAME, **context)
