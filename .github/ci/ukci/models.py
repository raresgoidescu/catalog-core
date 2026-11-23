from __future__ import annotations

import hashlib
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["PASS", "FAIL", "TIMEOUT", "BLOCKED", "MISSING"]
Phase = Literal["build", "run"]
Kind = Literal["first", "retry", "base", "base_retry"]
VerdictName = Literal["PASS", "FLAKY", "BROKEN_ON_BASE", "REGRESSION", "FAIL", "INFRA"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PullRequest(Model):
    repo: str
    number: int
    state: Literal["open", "closed", "merged"]
    base_ref: str
    head_sha: str
    mergeable: bool | None
    merge_sha: str | None
    merge_base_sha: str | None
    author: str
    association: str
    title: str = ""
    body: str = ""
    commit_messages: list[str] = []

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.number}".lower()

    @property
    def url(self) -> str:
        return f"https://github.com/{self.repo}/pull/{self.number}"


class Part(Model):
    label: str
    sha: str


class LockEntry(Model):
    repo: str
    sha: str
    source: str
    pr: str | None = None
    base_sha: str | None = None
    parts: list[Part] = []


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
    via: str
    used: bool
    note: str = ""


class CatalogRef(Model):
    repo: str
    sha: str
    source: str


class Reused(Model):
    run_url: str
    conclusion: str
    title: str
    summary: str


class Plan(Model):
    event: str = ""
    run_url: str = ""
    root: str | None = None
    root_repo: str | None = None
    root_head_sha: str | None = None
    recheck: bool = False
    channel: str = "staging"
    trusted: bool = True
    untrusted: list[str] = []
    errors: list[str] = []
    notes: list[str] = []
    skip: str = ""
    fan_out: bool = False
    then: list[str] = []
    prs: list[PlannedPR] = []
    catalog: CatalogRef | None = None
    catalog_base: CatalogRef | None = None
    untested_apps: list[str] = []
    cells: list[Cell] = []
    lock: dict[str, LockEntry] = {}
    base_lock: dict[str, LockEntry] = {}
    plan_hash: str = ""
    reused: Reused | None = None

    @property
    def n_configs(self) -> int:
        return sum(len(cell.variants) for cell in self.cells)

    def has_base(self, cell: Cell) -> bool:
        if self.catalog_base is not None:
            return True
        return any(path in self.lock and self.base_lock[path].sha != self.lock[path].sha for path in cell.repos)


class TamperedPlan(Exception):
    pass


def load_plan(path: str) -> Plan:
    with open(path, "rb") as f:
        raw = f.read()
    expected = os.environ.get("PLAN_DIGEST")
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise TamperedPlan(f"{path} is not the plan the plan job wrote")
    return Plan.model_validate_json(raw)


class Attempt(Model):
    variant: str = Field(max_length=100)
    attempt: Kind
    build: Status
    run: Status | None = None
    build_s: float | None = None
    run_s: float | None = None
    total_s: float | None = None
    reason: str = Field("", max_length=400)
    fetch_error: bool = False
    env_error: bool = False
    source: str = Field("", max_length=200)

    @property
    def ok(self) -> bool:
        return self.build == "PASS" and self.run in (None, "PASS")

    @property
    def phase(self) -> Phase:
        if self.build != "PASS":
            return "build"
        return "run"


class Result(Model):
    app: str = Field(max_length=100)
    compiler: str = Field(max_length=40)
    plan_hash: str = Field(max_length=64)
    tools: dict[str, str] = {}
    attempts: list[Attempt] = Field([], max_length=200)
    infra_error: str | None = Field(None, max_length=400)
    new_variants: list[str] = []


class Verdict(Model):
    app: str
    compiler: str
    variant: str
    verdict: VerdictName
    reason: str = ""


class Report(Model):
    plan_hash: str
    conclusion: str
    title: str
    verdicts: list[Verdict]
