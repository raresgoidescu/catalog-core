# catalog-core CI

Pull requests to Unikraft and its libraries (`unikraft`, `lib-*`, `app-*`)
are built and run on the apps in this repository. The result shows up on
the PR as the **Catalog Core** check.

## For contributors

**What gets tested.** Every app whose `setup.sh` links a repository your PR
changes, with every compiler (gcc, clang), on every variant its
`.scripts/test/all.sh` lists. A `unikraft` PR tests all apps. The check
lists which apps run and why.

**PRs that need each other.** Add a line to the PR description (or to a
commit message):

```
Depends-On: unikraft/lib-musl#12
Depends-On: lib-lwip#40, #7
Depends-On: https://github.com/unikraft/unikraft/pull/1745
```

`repo#N` is a repo in the same organization, `#N` is the same repo. Only
lines starting with `Depends-On:` count; "needs #12" in prose doesn't.
Dependencies are followed transitively; chains, diamonds and PRs that
depend on each other (A ↔ B) all work.

- Several PRs **in the same repo**: if they are stacked, CI uses the top
  one; otherwise it merges them together onto the target branch. If they
  conflict, the check names the PRs and files and you rebase one onto the
  other.
- All PRs must target the same branch (`staging` or `stable`).
- Already-merged dependencies are skipped. Closed ones are an error.
- Each PR is tested as GitHub's merge of it into its target branch.
- When a PR you depend on changes, your PR is tested again.

**Reading the result.** A *configuration* is one app + compiler + variant
(nginx, gcc, `qemu.x86_64`). The check has one row per verdict and first
error, linked to the job logs.

| | verdict | meaning | blocks? |
|---|---|---|---|
| ❌ | regression | passes without your PRs, or fails there only later (builds, but doesn't boot) | yes |
| ❌ | failure | fails, and there is nothing to compare it to (scheduled runs, a new app) | yes |
| ⚠️ | CI error | CI couldn't tell: downloads failed twice, a job died, the comparison is missing. Not your fault: `recheck` | yes |
| 🟡 | flaky | failed, then passed on retry with the same code | no |
| ⚪ | already broken | fails without your PRs too, in the same phase (or earlier) | no |
| ✅ | passed | | |

How it decides: a failed *run* is retried once, and so is a build that
failed downloading sources. What still fails is built and run once more
without your PRs. Only the phase (build or run) is compared, never the
error text, because parallel builds print errors in a different order every
time.

**Re-running.** Comment `recheck` on the PR (members and the PR's author).
Pushing, or editing the description, also re-runs it. If the exact same
commits were already tested, the check shows that result instead of
running again; `recheck` always runs.

**Reproducing locally.**

```sh
./setup.sh                          # clone the dependencies (staging)
cd nginx && ./.scripts/test/all.sh  # what CI runs, all variants
```

## For maintainers

**Approving runs.** PRs by authors who aren't trusted (see
`CI_TRUSTED_ASSOCIATIONS`), including any PR in their `Depends-On` set, only
run after a maintainer approves the `ci-needs-approval` environment in the
run linked from the check. Every new push needs a new approval. Declining
shows up on the PR as "⛔ Not run".

**Adding an app.** Add its directory. CI reads its dependencies from
`setup.sh` (`check_exists_and_create_symlink "<path>"`) and its variants
from `.scripts/test/all.sh` (`test_build <variant>` / `test_build_run
<variant>`). The `lint` workflow fails if it can't parse either. An app
without `.scripts/test/all.sh` is listed as untested, not failed.

**Adding a dependency repo.** Copy `.github/workflows/catalog-core-dispatch.yaml`
from any dependency repo (it is identical everywhere) and give the repo the
`APP_ID` variable and `PRIVATE_KEY` secret. Repo names follow
`libs/<x>` ↔ `lib-<x>`, `apps/<x>` ↔ `app-<x>`.

**Nightly and weekly.** Every night all apps are tested on `staging`, every
week on `stable` (the cron lines in `catalog-test.yml`). Discord hears
about it only when a configuration starts or stops failing, or when the run
couldn't run at all. Flaky results and CI errors don't count as changes.

**Settings** (repository variables and secrets; all variables optional):

| name | default | |
|---|---|---|
| `CI_TRUSTED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | whose PRs run without approval, and who may `recheck` |
| `CI_MAX_DEP_PRS` | `8` | limit on PRs tested together |
| `DISCORD_WEBHOOK_URL` (secret) | – | nightly notifications; none if unset |
| `DISCORD_MENTION` | – | e.g. `<@&role-id>`: pinged on new failures, and nothing else can ping |
| `APP_ID`, `PRIVATE_KEY` (secret) | – | the GitHub App that sends dispatches and writes checks |

**Deploying** (things the workflows rely on):

- The GitHub App has `checks: write` on the dependency repos and `contents:
  write` on catalog-core (needed for `repository_dispatch`). Protect `main`
  with a ruleset that the App can't bypass: its key is in every dependency
  repo.
- The `ci-needs-approval` environment exists and has required reviewers.
  (GitHub creates a missing environment *without* protection on first use.)
- Actions → "Approval for running fork pull request workflows" is set to
  *all external contributors*. A pull request to catalog-core itself runs
  its own version of these workflows, so for those, GitHub's approval is
  the gate (such runs get no secrets and a read-only token).
- Pages is enabled with source "GitHub Actions" for the dashboard.

## How it works

`.github/workflows/catalog-test.yml`:

1. **plan** (trusted, never runs PR code): `ukci plan` reads the PR(s) from
   the API, resolves `Depends-On`, pins one commit per repo (`lock`) and the
   same without the PRs (`base_lock`), and picks the apps. Then `ukci
   check-start` puts the check on the PR. If planning fails, the check says
   so.
2. **fan-out** (trusted): re-tests the PRs that were tested together with
   this one, once (dependents don't fan out further).
3. **test** (one job per app and compiler, no permissions or secrets): `ukci
   run` runs each variant through the app's own `all.sh`, retries, and runs
   failures without the PRs. Output: `result.json`.
4. **report** (trusted): `ukci report` turns the results into verdicts,
   finishes the check, records the run on the `ci-data` branch, and tells
   Discord after scheduled runs.

The test job can write any `result.json` it likes: it runs the PR's code as
root. What keeps that honest is the approval gate, not the report job; the
report job only makes sure a bad file can't crash it or inject into the
check. Test jobs can also replace the run's artifacts, so the trusted jobs
accept `plan.json` only if it matches the digest the plan job published as
a job output.

`ci-data` holds one file per run (`runs/`), the result per exact set of
commits (`plans/`, for reuse), who was tested with whom (`deps/`, for
fan-out) and the last nightly state (`nightly/`). The dashboard is built
from it.

## Developing

```sh
cd .github/ci
uv sync                                    # Python and dependencies, from uv.lock
uv run pytest                              # tests
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python -m ukci --root ../.. apps    # what CI reads from the app directories
uv run python -m ukci --root ../.. run --plan plan.json --app c-hello --compiler gcc --no-sudo
```

| module | |
|---|---|
| `models.py` | the data passed between jobs (`plan.json`, `result.json`) |
| `apps.py`, `repos.py` | apps, their dependencies and variants; repo names |
| `resolve.py`, `plan.py` | Depends-On, then what to test at which commits |
| `run.py` | one app and compiler, variant by variant |
| `verdict.py` | results → verdicts |
| `report.py`, `notify.py`, `render.py` | the check, Discord, what people read |
| `github.py`, `git.py`, `data.py` | `gh`, git, and the `ci-data` branch |
