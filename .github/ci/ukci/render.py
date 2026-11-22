from __future__ import annotations

import collections

import jinja2

from .models import LockEntry, Plan, Verdict, VerdictName
from .verdict import BLOCKING, ORDER

NAME = {
    "REGRESSION": ("regression", "regressions"),
    "FAIL": ("failure", "failures"),
    "INFRA": ("CI error", "CI errors"),
    "FLAKY": ("flaky", "flaky"),
    "BROKEN_ON_BASE": ("already broken", "already broken"),
    "PASS": ("passed", "passed"),
}
MAX_SUMMARY = 60000
README = ".github/ci/README.md"


def plural(n: int, one: str, many: str | None = None) -> str:
    if n == 1:
        return f"{n} {one}"
    return f"{n} {many or one + 's'}"


def oneline(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def cell(text: str) -> str:
    return oneline(text, 300).replace("|", "\\|").replace("[", "\\[").replace("<", "&lt;")


def code(text: str) -> str:
    if not text:
        return ""
    return "`" + oneline(text, 300).replace("|", "\\|").replace("`", "'") + "`"


def label(name: VerdictName) -> str:
    word = NAME[name][0]
    if name in BLOCKING:
        return f"**{word}**"
    return word


def title(plan: Plan, verdicts: list[Verdict]) -> str:
    if plan.errors:
        more = ""
        if len(plan.errors) > 1:
            more = f" (+{len(plan.errors) - 1} more)"
        return f"Not tested: {oneline(plan.errors[0], 180)}{more}"
    if not plan.cells:
        return "No app uses the changed repositories"
    count = collections.Counter(v.verdict for v in verdicts)
    if count["PASS"] == len(verdicts):
        return f"All {plural(len(verdicts), 'configuration')} passed"
    parts = []
    for name in ORDER[:-1]:
        if count[name]:
            parts.append(f"{count[name]} {NAME[name][count[name] != 1]}")
    parts.append(f"{count['PASS']} of {len(verdicts)} passed")
    return ", ".join(parts)


def describe(configs: list[tuple[str, str, str]]) -> str:
    by_job: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for app, compiler, variant in sorted(configs):
        by_job[app, compiler].append(variant)
    return "; ".join(f"{app} {compiler}: {', '.join(variants)}" for (app, compiler), variants in by_job.items())


def grid(verdicts: list[Verdict], logs: dict[tuple[str, str], str]) -> list[str]:
    variants = sorted({v.variant for v in verdicts})
    rows: dict[tuple[str, str], dict[str, VerdictName]] = collections.defaultdict(dict)
    for v in verdicts:
        rows[v.app, v.compiler][v.variant] = v.verdict
    lines = ["| app | compiler | " + " | ".join(variants) + " |", "|---|---|" + "---|" * len(variants)]
    for (app, compiler), by_variant in sorted(rows.items()):
        name = app
        if (app, compiler) in logs:
            name = f"[{app}]({logs[app, compiler]})"
        marks = []
        for variant in variants:
            if variant in by_variant:
                marks.append(label(by_variant[variant]))
            else:
                marks.append("")
        lines.append(f"| {name} | {compiler} | " + " | ".join(marks) + " |")
    lines.append("")
    return lines


def errors_table(verdicts: list[Verdict]) -> list[str]:
    groups: dict[tuple[VerdictName, str], list[tuple[str, str, str]]] = collections.defaultdict(list)
    for v in verdicts:
        if v.verdict != "PASS":
            groups[v.verdict, v.reason].append((v.app, v.compiler, v.variant))
    if not groups:
        return []
    ordered = sorted(groups.items(), key=lambda item: (ORDER.index(item[0][0]), -len(item[1])))
    lines = ["| verdict | first error | configurations |", "|---|---|---|"]
    for (name, reason), configs in ordered:
        where = describe(configs)
        if len(configs) == len(verdicts):
            where = f"all {len(configs)}"
        lines.append(f"| {label(name)} | {code(reason)} | {where} |")
    lines.append("")
    return lines


def tested_as(plan: Plan) -> dict[str, str]:
    by_pr: dict[str, LockEntry] = {}
    for entry in plan.lock.values():
        if entry.pr:
            for ref in entry.pr.split(" + "):
                by_pr[ref] = entry
    out = {}
    for pr in plan.prs:
        locked = by_pr.get(pr.ref)
        if not pr.used:
            out[pr.ref] = f"not used: {pr.note}"
        elif locked is None:
            out[pr.ref] = "not needed by the tested apps"
        elif locked.parts:
            others = ", ".join(part.label for part in locked.parts if part.label != pr.ref)
            out[pr.ref] = f"`{locked.sha[:10]}`, merged with {others} into `{pr.base_ref}`"
        else:
            out[pr.ref] = f"`{locked.sha[:10]}`, merged into `{pr.base_ref}`"
    return out


def check(
    plan: Plan,
    verdicts: list[Verdict] | None = None,
    running: bool = False,
    banner: str = "",
    logs: dict[tuple[str, str], str] | None = None,
) -> str:
    lines = []
    quoted = list(plan.notes)
    if banner:
        quoted.insert(0, banner)
    for note in quoted:
        lines.append(f"> {cell(note)}")
    if quoted:
        lines.append("")
    if plan.errors:
        lines += ["**Not tested**", ""]
        lines += [f"- {oneline(error, 1000)}" for error in plan.errors]
        lines.append("")
    if running and not plan.trusted:
        lines.append(
            f"**Waiting for a maintainer to [approve the run]({plan.run_url})**: some PRs are by authors "
            "CI doesn't trust yet, and none of their code runs before that."
        )
        lines.append("")
        lines += [f"- {cell(who)}" for who in plan.untrusted]
        lines.append("")
    if running and plan.cells:
        by_app: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
        for c in plan.cells:
            by_app[c.app, c.why].append(c.compiler)
        lines += [f"**Testing {plural(plan.n_configs, 'configuration')}**", ""]
        for (app, why), compilers in sorted(by_app.items()):
            lines.append(f"- {app} ({', '.join(compilers)}): {why}")
        lines.append("")
    if verdicts:
        lines += grid(verdicts, logs or {})
        lines += errors_table(verdicts)
    if plan.prs:
        how = tested_as(plan)
        lines += ["| PR | tested as | found via |", "|---|---|---|"]
        for pr in plan.prs:
            lines.append(f"| [{pr.ref}]({pr.url}) {cell(pr.title[:60])} | {cell(how[pr.ref])} | {cell(pr.via)} |")
        lines.append("")
    if plan.lock:
        lines += ["<details><summary>Exact commits</summary>", "", "| repo | commit | from |", "|---|---|---|"]
        if plan.catalog:
            lines.append(f"| catalog-core | `{plan.catalog.sha[:10]}` | {cell(plan.catalog.source)} |")
        for path, entry in sorted(plan.lock.items()):
            lines.append(f"| {path} ({entry.repo}) | `{entry.sha[:10]}` | {cell(entry.source)} |")
        lines += ["", "</details>", ""]
    footer = [f"[Workflow run]({plan.run_url})"]
    if plan.catalog:
        footer.append(f"[how to read this](https://github.com/{plan.catalog.repo}/blob/{plan.catalog.sha}/{README})")
    footer.append(f"plan `{plan.plan_hash[:10]}`")
    lines.append(" · ".join(footer))
    text = "\n".join(lines)
    if len(text) < MAX_SUMMARY:
        return text
    return text[:MAX_SUMMARY] + "\n\n(truncated, see the workflow run)"


def reused(plan: Plan) -> str:
    assert plan.reused
    note = (
        f"> These exact commits were already tested in [this run]({plan.reused.run_url}); its result is "
        "shown below. Comment `recheck` to run the tests again.\n\n"
    )
    return note + plan.reused.summary


TEMPLATES = jinja2.Environment(
    loader=jinja2.PackageLoader("ukci", "templates"), autoescape=True, undefined=jinja2.StrictUndefined
)


def dashboard(**context) -> str:
    return TEMPLATES.get_template("dashboard.html.j2").render(name=NAME, **context)
