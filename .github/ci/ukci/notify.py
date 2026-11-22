"""Discord: after scheduled runs only, one message, only when something changed."""

from __future__ import annotations

import json
import os
import urllib.request

from . import render
from .data import Store
from .models import Plan, Verdict

BAD = {"FAIL", "INFRA", "REGRESSION"}


def changes(previous: dict[str, str] | None, verdicts: list[Verdict]) -> tuple[list[str], list[str]]:
    """(newly failing, fixed) configurations compared to the previous scheduled run.

    Flaky counts as passing, so a flaky configuration doesn't toggle the channel.
    New configurations are not reported until they have a previous result.
    """
    prev = previous or {}
    broke, fixed = [], []
    for v in verdicts:
        key = f"{v.app} {v.compiler} {v.variant}"
        if key not in prev:
            continue
        if v.verdict in BAD and prev[key] not in BAD:
            broke.append(f"{key}: {v.reason}")
        elif v.verdict not in BAD and prev[key] in BAD:
            fixed.append(key)
    return broke, fixed


def post(webhook: str, text: str, mention: str = "") -> None:
    body = {
        "content": f"{mention}\n{text}" if mention else text,
        # Only the configured mention may ping; nothing from test output can.
        "allowed_mentions": {"parse": ["roles", "users"] if mention else []},
    }
    req = urllib.request.Request(
        webhook,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "catalog-core-ci"},
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # a notification never fails the run
        print(f"::warning::Discord notification failed: {e}")


def nightly(plan: Plan, verdicts: list[Verdict], title: str, store: Store | None) -> None:
    previous = store.nightly(plan.channel) if store else None
    broke, fixed = changes(previous, verdicts)
    if store:
        store.set_nightly(plan, verdicts)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        return
    if previous is not None and not broke and not fixed:
        print("nightly: nothing changed since the previous run; no Discord message")
        return
    mention = os.environ.get("DISCORD_MENTION", "") if broke else ""
    post(webhook, render.discord(plan, title, broke, fixed, first=previous is None), mention)
