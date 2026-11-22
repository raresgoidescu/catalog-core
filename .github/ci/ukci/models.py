"""The data passed between jobs: plan.json (plan job -> test and report jobs)
and result.json (test job -> report job).

result.json comes from jobs that ran PR code, so it is untrusted input:
the report job only accepts it through these models.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["PASS", "FAIL", "TIMEOUT", "BLOCKED", "MISSING"]
VerdictName = Literal["PASS", "FLAKY", "BROKEN_ON_BASE", "REGRESSION", "FAIL", "INFRA"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- GitHub ------------------------------------------------------------------


class PullRequest(Model):
    repo: str  # base repository, "owner/name"
    number: int
    state: Literal["open", "closed", "merged"]
    base_ref: str
    head_sha: str
    mergeable: bool | None  # None: GitHub hasn't computed it yet
    merge_sha: str | None  # GitHub's test merge of head into base
    merge_base_sha: str | None  # the base commit that test merge was made onto
    author: str
    association: str  # OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | ...
    title: str = ""
    body: str = ""
    commit_messages: list[str] = []

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}".lower()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


# --- plan.json ---------------------------------------------------------------


class Part(Model):
    label: str  # "owner/repo#N"
    sha: str  # PR head


class LockEntry(Model):
    repo: str
    sha: str
    source: str  # for people: "staging", "o/r#12 merged into staging", ...
    pr: str | None = None  # "o/r#12", or "o/r#12 + o/r#15" when combined
    base_sha: str | None = None  # combined: the commit the parts were merged onto
    parts: list[Part] = []  # combined: the PR heads, in merge order


class Cell(Model):
    app: str
    compiler: str
    why: str
    repos: list[str]
    variants: list[str]


class PlannedPR(Model):
    ref: str
    url: str
    title: str
    head_sha: str
    base_ref: str
    via: str  # how it was found
    used: bool  # False: covered by a PR stacked on top
    note: str = ""  # why it isn't used


class CycleMember(Model):
    ref: str
    repo: str
    head_sha: str


class CatalogRef(Model):
    repo: str
    sha: str
    source: str


class Plan(Model):
    version: Literal[2] = 2
    event: str = ""
    run_url: str = ""
    root: str | None = None  # "owner/repo#N"; None for scheduled/manual runs
    root_repo: str | None = None
    root_head_sha: str | None = None
    channel: str = "staging"
    trusted: bool = True
    untrusted: list[str] = []
    errors: list[str] = []
    notes: list[str] = []
    prs: list[PlannedPR] = []
    cycle: list[CycleMember] = []  # PRs that resolve to this same set (incl. the root)
    delegate_to: str | None = None  # tested in that PR's run instead
    root_closed: bool = False
    fan_out: bool = False
    catalog: CatalogRef | None = None
    catalog_base: CatalogRef | None = None  # catalog-core without a catalog-core PR
    untested_apps: list[str] = []
    cells: list[Cell] = []
    lock: dict[str, LockEntry] = {}
    base_lock: dict[str, LockEntry] = {}
    plan_hash: str = ""

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def n_configs(self) -> int:
        return sum(len(c.variants) for c in self.cells)

    def has_base(self, cell: Cell) -> bool:
        """Is there anything 'without the PRs' to compare this cell to?"""
        return self.catalog_base is not None or any(
            self.base_lock[p].sha != self.lock[p].sha for p in cell.repos if p in self.lock
        )


# --- result.json ---------------------------------------------------------------


class Attempt(Model):
    variant: str
    attempt: Literal["first", "retry", "base"]
    build: Status
    run: Status | None = None  # None: build-only variant
    build_s: float | None = None
    run_s: float | None = None
    total_s: float | None = None
    reason: str = Field("", max_length=400)  # first error line, for people
    errors: list[str] = Field([], max_length=30)  # every distinct error, normalized, for comparing
    fetch_error: bool = False

    @property
    def ok(self) -> bool:
        return self.build == "PASS" and self.run in (None, "PASS")

    def same_failure(self, other: Attempt) -> bool:
        """Do both fail in the same way? With parallel builds the first error is a matter of
        timing, so: the same phase, and at least one error in common."""
        return bool(self.errors) and self.phase == other.phase and bool(set(self.errors) & set(other.errors))

    @property
    def phase(self) -> str:
        return "build" if self.build != "PASS" else "run"


class Result(Model):
    app: str
    compiler: str
    plan_hash: str
    tools: dict[str, str] = {}
    attempts: list[Attempt] = []
    infra_error: str | None = None
    new_variants: list[str] = []  # added by a catalog-core PR: nothing to compare to


# --- verdicts ------------------------------------------------------------------


class Verdict(Model):
    app: str
    compiler: str
    variant: str
    verdict: VerdictName
    reason: str = ""  # what failed
    detail: str = ""  # why this verdict
    inherited_from: str | None = None  # dependency PR whose own run fails the same way


class Report(Model):
    plan_hash: str
    conclusion: str
    title: str
    verdicts: list[Verdict]
