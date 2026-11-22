"""Everything people read: check titles and summaries, Discord, the dashboard.

The wording lives in templates/*.j2; this module only prepares what they show.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass

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


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def md_cell(text: str) -> str:
    """Safe inside a markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


_markdown = jinja2.Environment(
    loader=jinja2.PackageLoader("ukci", "templates"),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=jinja2.StrictUndefined,
)
_markdown.filters.update(short=lambda s: (s or "")[:10], plural=plural, cell=md_cell)
_html = jinja2.Environment(
    loader=jinja2.PackageLoader("ukci", "templates"), autoescape=True, undefined=jinja2.StrictUndefined
)


@dataclass
class Group:
    """One distinct failure and the configurations it hits."""

    verdict: VerdictName
    reason: str
    detail: str
    configs: list[tuple[str, str, str]]

    @property
    def icon(self) -> str:
        return ICON[self.verdict]

    @property
    def label(self) -> str:
        return NAME[self.verdict][0].capitalize()

    @property
    def where(self) -> str:
        return describe(self.configs)


def describe(configs: list[tuple[str, str, str]]) -> str:
    """(app, compiler, variant) list, compactly: a cross product when it is one."""
    apps, ccs, vs = (sorted({c[i] for c in configs}) for i in range(3))
    if len(configs) == len(apps) * len(ccs) * len(vs):
        return " × ".join(", ".join(x) for x in (apps, ccs, vs))
    by_app = collections.defaultdict(list)
    for a, c, v in sorted(configs):
        by_app[a].append(f"{c} {v}")
    return "; ".join(f"{a}: {', '.join(x)}" for a, x in by_app.items())


def groups(verdicts: list[Verdict]) -> list[Group]:
    by: dict[tuple, list] = collections.defaultdict(list)
    for v in verdicts:
        if v.verdict != "PASS":
            by[v.verdict, v.reason, v.detail].append((v.app, v.compiler, v.variant))
    out = [Group(verdict, reason, detail, configs) for (verdict, reason, detail), configs in by.items()]
    return sorted(out, key=lambda g: (ORDER.index(g.verdict), -len(g.configs)))


def title(plan: Plan, verdicts: list[Verdict]) -> str:
    if plan.errors:
        more = f" (+{len(plan.errors) - 1} more)" if len(plan.errors) > 1 else ""
        return f"❌ Not tested: {plan.errors[0][:180]}{more}"
    if not plan.cells:
        return "✅ No app uses the changed repositories"
    count = collections.Counter(v.verdict for v in verdicts)
    if count["PASS"] == len(verdicts):
        return f"✅ All {plural(len(verdicts), 'configuration')} passed"
    parts = [f"{count[k]} {NAME[k][count[k] != 1]}" for k in ORDER[:-1] if count[k]]
    sources = {v.inherited_from for v in verdicts if v.verdict == "REGRESSION"}
    if count["REGRESSION"] and None not in sources:
        parts[0] += f" (inherited from {', '.join(sorted(s for s in sources if s))})"
    return f"{ICON[next(k for k in ORDER if count[k])]} " + " · ".join(
        [*parts, f"{count['PASS']}/{len(verdicts)} passed"]
    )


def tested_as(plan: Plan) -> dict[str, str]:
    """PR -> how its code went into the test, for the "Tested together" table."""
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
            out[pr.ref] = f"`{e.sha[:10]}`: merged together with {others} into `{pr.base_ref}`"
        else:
            out[pr.ref] = f"`{e.sha[:10]}`: merged into `{pr.base_ref}`"
    return out


def check(plan: Plan, verdicts: list[Verdict] | None = None, *, running: bool = False, banner: str = "") -> str:
    """The check summary (also the workflow run summary)."""
    passed = [(v.app, v.compiler, v.variant) for v in verdicts or [] if v.verdict == "PASS"]
    apps: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for c in plan.cells:
        apps[c.app, c.why].append(c.compiler)
    text = _markdown.get_template("check.md.j2").render(
        tested_as=tested_as(plan),
        plan=plan,
        running=running,
        banner=banner,
        groups=groups(verdicts or []),
        passed=describe(passed) if passed else "",
        n_passed=len(passed),
        apps=sorted(apps.items()),
        show_untested=not plan.root,
    )
    return text if len(text) < MAX_SUMMARY else text[:MAX_SUMMARY] + "\n\n…(truncated, see the workflow run)"


def discord(plan: Plan, title_text: str, broke: list[str], fixed: list[str], first: bool) -> str:
    return _markdown.get_template("discord.md.j2").render(
        plan=plan, title=title_text, broke=broke[:15], fixed=fixed[:15], first=first
    )[:1900]


def dashboard(**context) -> str:
    return _html.get_template("dashboard.html.j2").render(icon=ICON, name=NAME, **context)
