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
NOT_A_CHANGE = {"INFRA", "FLAKY"}
MAX_LINES = 15


def next_state(previous: dict | None, verdicts: list[Verdict], error: str, run_url: str) -> dict:
    cells = dict((previous or {}).get("cells") or {})
    if not error:
        for v in verdicts:
            if v.verdict not in NOT_A_CHANGE:
                cells[config_key(v)] = v.verdict
    return {"run_url": run_url, "error": error, "cells": cells}


def changes(previous: dict | None, verdicts: list[Verdict]) -> tuple[list[str], list[str]]:
    if previous is None:
        return [], []
    before = previous.get("cells") or {}
    broke = []
    fixed = []
    for v in verdicts:
        key = config_key(v)
        if v.verdict in NOT_A_CHANGE:
            continue
        if v.verdict in FAILING and before.get(key) not in FAILING:
            broke.append(f"{key}: {v.reason}")
        elif v.verdict not in FAILING and before.get(key) in FAILING:
            fixed.append(key)
    return broke, fixed


def allowed_mentions(mention: str) -> dict:
    return {
        "parse": [],
        "roles": re.findall(r"<@&(\d+)>", mention),
        "users": re.findall(r"<@!?(\d+)>", mention),
    }


def post(webhook: str, text: str, mention: str = "") -> bool:
    content = text
    if mention:
        content = f"{mention}\n{text}"
    body = {"content": content[:2000], "allowed_mentions": allowed_mentions(mention)}
    headers = {"Content-Type": "application/json", "User-Agent": "catalog-core-ci"}
    request = urllib.request.Request(webhook, data=json.dumps(body).encode(), method="POST", headers=headers)
    for attempt in range(3):
        try:
            urllib.request.urlopen(request, timeout=20).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                print(f"::warning::Discord refused the notification: HTTP {e.code}")
                return False
        except OSError as e:
            print(f"::warning::Discord notification failed: {e}")
        time.sleep(5 * (attempt + 1))
    return False


def message(
    plan: Plan, title: str, previous: dict | None, broke: list[str], fixed: list[str], error: str
) -> str | None:
    head = f"**catalog-core nightly ({plan.channel})**"
    if error:
        if previous and previous.get("error"):
            return None
        return f"{head}: couldn't run: {error}\n{plan.run_url}"
    if previous is None:
        return f"{head}: {title}\n{plan.run_url}"
    if not broke and not fixed and not previous.get("error"):
        return None
    lines = [f"{head}: {title}"]
    if previous.get("error"):
        lines.append("Running again after an error.")
    for heading, items in (("Newly failing", broke), ("Fixed", fixed)):
        if not items:
            continue
        lines.append(f"**{heading}**")
        for item in items[:MAX_LINES]:
            lines.append(f"- {render.oneline(item)}")
        if len(items) > MAX_LINES:
            lines.append(f"- and {len(items) - MAX_LINES} more")
    lines.append(plan.run_url)
    return "\n".join(lines)


def nightly(plan: Plan, verdicts: list[Verdict], title: str, store: Store | None, error: str = "") -> None:
    previous = store.nightly(plan.channel) if store else None
    broke, fixed = changes(previous, verdicts)
    text = message(plan, title, previous, broke, fixed, error)
    if store:
        store.set_nightly(plan.channel, next_state(previous, verdicts, error, plan.run_url))
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if text is None:
        print("nightly: nothing changed since the previous run; no Discord message")
        return
    if not webhook:
        return
    mention = ""
    if broke:
        mention = os.environ.get("DISCORD_MENTION", "")
    post(webhook, text, mention)
