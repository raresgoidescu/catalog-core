"""Build a static dashboard (one HTML file) from a ci-data checkout."""

from __future__ import annotations

import glob
import html
import json
import os

COLORS = {"PASS": "pass", "FLAKY": "flaky", "BROKEN_ON_BASE": "base", "REGRESSION": "bad",
          "FAIL": "bad", "INFRA": "infra"}
TITLE = {"PASS": "passed", "FLAKY": "flaky", "BROKEN_ON_BASE": "already broken on base",
         "REGRESSION": "regression", "FAIL": "failed", "INFRA": "CI error"}

CSS = """
:root{--bg:#fff;--fg:#1f2328;--muted:#59636e;--line:#d1d9e0;--pass:#1a7f37;--bad:#cf222e;--flaky:#bf8700;--base:#8c959f;--infra:#8250df;--none:#eaeef2}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--muted:#9198a1;--line:#30363d;--pass:#3fb950;--bad:#f85149;--flaky:#d29922;--base:#6e7681;--infra:#a371f7;--none:#21262d}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0;padding:24px 16px;max-width:1200px;margin-inline:auto}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}p.m{color:var(--muted);margin:0 0 16px}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%}td,th{padding:4px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{font-weight:600;color:var(--muted);font-size:12px}
.grid td.c{padding:2px}.dot{display:block;width:12px;height:12px;border-radius:3px;background:var(--none)}
.pass{background:var(--pass)}.bad{background:var(--bad)}.flaky{background:var(--flaky)}.base{background:var(--base)}.infra{background:var(--infra)}
.legend span{display:inline-flex;align-items:center;gap:4px;margin-right:12px;color:var(--muted)}
a{color:inherit}.ok{color:var(--pass)}.ko{color:var(--bad)}
"""


def load_runs(root: str) -> list[dict]:
    runs = []
    for path in glob.glob(os.path.join(root, "runs", "*", "*", "*", "*.json")):
        try:
            with open(path) as f:
                runs.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
    return sorted(runs, key=lambda r: r.get("time", ""))


def grid(runs: list[dict], channel: str, n: int = 30) -> str:
    sched = [r for r in runs if r.get("event") == "schedule" and r.get("channel") == channel][-n:]
    if not sched:
        return f"<p class=m>No scheduled {channel} runs recorded yet.</p>"
    cells = sorted({(v["app"], v["compiler"], v["variant"]) for r in sched for v in r.get("verdicts", [])})
    head = "".join(f'<th><a href="{html.escape(r.get("run_url", ""))}" title="{html.escape(r.get("time", ""))}">'
                   f'{html.escape(r.get("time", "")[5:10])}</a></th>' for r in sched)
    rows = []
    for c in cells:
        tds = []
        for r in sched:
            v = next((x for x in r.get("verdicts", []) if (x["app"], x["compiler"], x["variant"]) == c), None)
            cls = COLORS.get(v["verdict"], "") if v else ""
            tip = html.escape(f"{TITLE.get(v['verdict'], v['verdict'])}: {v.get('detail', '')}") if v else "not run"
            tds.append(f'<td class=c><span class="dot {cls}" title="{tip}"></span></td>')
        rows.append(f"<tr><td>{html.escape(c[0])}</td><td>{html.escape(c[1])}</td><td>{html.escape(c[2])}</td>{''.join(tds)}</tr>")
    return (f'<div class=scroll><table class=grid><tr><th>app</th><th>compiler</th><th>variant</th>{head}</tr>'
            f'{"".join(rows)}</table></div>')


def pr_runs(runs: list[dict], n: int = 40) -> str:
    prs = [r for r in runs if r.get("root")][-n:][::-1]
    if not prs:
        return "<p class=m>No PR runs recorded yet.</p>"
    rows = []
    for r in prs:
        minutes = sum((t.get("total_s") or 0) for t in r.get("timings", [])) / 60
        ok = r.get("conclusion") == "success"
        together = ", ".join(p["pr"] for p in r.get("prs", []) if p.get("used") and p["pr"] != r["root"])
        rows.append(f'<tr><td>{html.escape(r.get("time", "")[:16].replace("T", " "))}</td>'
                    f'<td><a href="https://github.com/{html.escape(r["root"].replace("#", "/pull/"))}">{html.escape(r["root"])}</a></td>'
                    f'<td>{html.escape(together) or "–"}</td>'
                    f'<td class={"ok" if ok else "ko"}><a href="{html.escape(r.get("run_url", ""))}">{html.escape(r.get("title", ""))}</a></td>'
                    f'<td>{minutes:.0f}</td></tr>')
    return ("<div class=scroll><table><tr><th>time (UTC)</th><th>PR</th><th>tested with</th><th>result</th>"
            f"<th>test minutes</th></tr>{''.join(rows)}</table></div>")


def flake_table(runs: list[dict]) -> str:
    counts: dict[tuple, list[int]] = {}
    for r in runs[-200:]:
        for v in r.get("verdicts", []):
            k = (v["app"], v["compiler"], v["variant"])
            c = counts.setdefault(k, [0, 0])
            c[1] += 1
            c[0] += v["verdict"] == "FLAKY"
    flaky = sorted(((f / t, f, t, k) for k, (f, t) in counts.items() if f), reverse=True)[:20]
    if not flaky:
        return "<p class=m>No flaky results in the last 200 runs.</p>"
    rows = "".join(f"<tr><td>{html.escape(' '.join(k))}</td><td>{f}/{t}</td><td>{rate:.0%}</td></tr>" for rate, f, t, k in flaky)
    return f"<div class=scroll><table><tr><th>cell</th><th>flaky / runs</th><th>rate</th></tr>{rows}</table></div>"


def build(root: str, out: str) -> None:
    runs = load_runs(root)
    legend = "".join(f'<span><i class="dot {c}"></i>{TITLE[k]}</span>' for k, c in COLORS.items() if k != "FAIL")
    page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>catalog-core CI</title><style>{CSS}</style>"
            f"<h1>catalog-core CI</h1><p class=m>{len(runs)} recorded runs · generated from the <code>ci-data</code> branch</p>"
            f"<div class=legend>{legend}</div>"
            f"<h2>Nightly: staging</h2>{grid(runs, 'staging')}"
            f"<h2>Weekly: stable</h2>{grid(runs, 'stable', 12)}"
            f"<h2>Pull requests</h2>{pr_runs(runs)}"
            f"<h2>Flaky cells</h2>{flake_table(runs)}")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "index.html"), "w") as f:
        f.write(page)


def main(args) -> int:
    build(args.data, args.out)
    print(f"wrote {args.out}/index.html")
    return 0
