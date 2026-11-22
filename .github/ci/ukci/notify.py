"""Discord: after scheduled runs only, one message, only when something changed.

A configuration is failing if it FAILs (scheduled runs have nothing to
compare to, so that is the only failing verdict). FLAKY counts as passing,
and INFRA ("CI couldn't tell") keeps the previous state, so neither toggles
the channel. A nightly that can't run at all is reported once, not nightly.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

from . import render
from .data import Store, config_key
from .models import Plan, Verdict

FAILING = {"FAIL", "REGRESSION"}
MAX_LINES = 15


def next_state(previous: dict | None, verdicts: list[Verdict], error: str, run_url: str) -> dict:
    cells = dict((previous or {}).get("cells") or {})
    if not error:
        for v in verdicts:
            if v.verdict != "INFRA":
                cells[config_key(v)] = v.verdict
    return {"run_url": run_url, "error": error, "cells": cells}


def changes(previous: dict | None, verdicts: list[Verdict]) -> tuple[list[str], list[str]]:
    """(newly failing, fixed) configurations; configurations seen for the first time don't count."""
    prev = (previous or {}).get("cells") or {}
    broke, fixed = [], []
    for v in verdicts:
        key = config_key(v)
        if key not in prev or v.verdict == "INFRA":
            continue
        if v.verdict in FAILING and prev[key] not in FAILING:
            broke.append(f"{key}: {v.reason}")
        elif v.verdict not in FAILING and prev[key] in FAILING:
            fixed.append(key)
    return broke, fixed


def allowed_mentions(mention: str) -> dict:
    """Only the configured role/user may be pinged; nothing from test output can."""
    return {
        "parse": [],
        "roles": re.findall(r"<@&(\d+)>", mention),
        "users": re.findall(r"<@!?(\d+)>", mention),
    }


def post(webhook: str, text: str, mention: str = "") -> bool:
    body = {
        "content": (f"{mention}\n{text}" if mention else text)[:2000],
        "allowed_mentions": allowed_mentions(mention),
    }
    req = urllib.request.Request(
        webhook,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "catalog-core-ci"},
    )
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                print(f"::warning::Discord refused the notification: HTTP {e.code}")
                return False
        except OSError as e:
            print(f"::warning::Discord notification failed: {e}")
        time.sleep(5 * (attempt + 1))
    return False  # a notification never fails the run


def message(plan: Plan, title: str, previous: dict | None, verdicts: list[Verdict], error: str) -> str | None:
    """The message for this nightly, or None if nothing changed."""
    head = f"**catalog-core nightly ({plan.channel})**"
    if error:
        if previous and previous.get("error"):
            return None
        return f"{head}: ⚠️ couldn't run: {error}\n{plan.run_url}"
    if previous is None:
        return f"{head}: first recorded run. {title}\n{plan.run_url}"
    broke, fixed = changes(previous, verdicts)
    if not broke and not fixed and not previous.get("error"):
        return None
    lines = [f"{head}: {title}"]
    if previous.get("error"):
        lines.append("Running again after an error.")
    for label, items in (("Newly failing", broke), ("Fixed", fixed)):
        if items:
            lines.append(f"**{label}**")
            lines += [f"• {render.oneline(x)}" for x in items[:MAX_LINES]]
            if len(items) > MAX_LINES:
                lines.append(f"• … and {len(items) - MAX_LINES} more")
    lines.append(plan.run_url)
    return "\n".join(lines)


def nightly(plan: Plan, verdicts: list[Verdict], title: str, store: Store | None, error: str = "") -> None:
    previous = store.nightly(plan.channel) if store else None
    text = message(plan, title, previous, verdicts, error)
    if store:
        store.set_nightly(plan.channel, next_state(previous, verdicts, error, plan.run_url))
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if text is None:
        print("nightly: nothing changed since the previous run; no Discord message")
    elif webhook:
        broke = bool(changes(previous, verdicts)[0])  # ping only for new failures
        post(webhook, text, os.environ.get("DISCORD_MENTION", "") if broke else "")
