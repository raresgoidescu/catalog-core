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

`repo#N` is a repo in the same organization, `#N` is the same repo. The
line may be a list item or bold (`- Depends-On: #7`, `**Depends-On:**
#7`), and `Depends on #7` at the start of a line counts too. Every PR on
the line counts; other words on it are ignored. "This needs #12" or "it
depends on #12" in the middle of a sentence doesn't count, and the check
says so. Dependencies are followed transitively; chains, diamonds and PRs
that depend on each other (A ↔ B) all work. The `Depends-On` lines of
already-merged dependencies are not followed.

- Several PRs **in the same repo**: if they are stacked, CI uses the top
  one; otherwise it merges them together onto the target branch. If they
  conflict, the check names the PRs and files and you rebase one onto the
  other.
- All PRs must target the same branch (`staging` or `stable`). A
  catalog-core PR targets `main` and is tested with the dependencies'
  branch.
- Already-merged dependencies are skipped. Closed ones are an error.
- Each PR is tested as GitHub's merge of it into its target branch.
- When a PR you depend on changes, your PR is tested again.

**Reading the result.** A *configuration* is one app + compiler + variant
(nginx, gcc, `qemu.x86_64`). The check shows a grid of apps and compilers
against variants, linked to the job logs, then one row per distinct error.

| verdict | meaning | blocks? |
|---|---|---|
| regression | passes without your PRs, or fails there only later (builds, but doesn't boot) | yes |
| failure | fails, and there is nothing to compare it to (scheduled runs, a new app) | yes |
| CI error | CI couldn't tell: downloads failed twice, the runner lacks a tool, a job died, the comparison is missing. Not your fault: `recheck` | yes |
| flaky | failed, then passed on retry with the same code | no |
| already broken | fails without your PRs too, in the same phase (or earlier) | no |
| passed | | |

How it decides: a failed *run* is retried once, and so is a build that
failed downloading sources. A run that takes longer than 5 minutes counts
as failed (a build gets 20). What still fails is built and run once more
without your PRs, and a run that fails there too is retried once. Only the
phase (build or run) is compared, never the
error text, because parallel builds print errors in a different order every
time. When a nightly already ran a configuration on exactly the commits
your PRs are based on (same toolchain too), its result is the comparison
and nothing is rebuilt.

**Re-running.** Comment `recheck` on the PR (members and the PR's author;
other people's comments are ignored). Pushing, or changing a line of the
description that mentions dependencies, also re-runs it. If the exact same
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
shows up on the PR as "Not run".

**Adding an app.** Add its directory. CI reads its dependencies from
`setup.sh` (`check_exists_and_create_symlink "<path>"`) and its variants
from `.scripts/test/all.sh` (`test_build <variant>` / `test_build_run
<variant>`). A catalog-core PR fails if CI can't parse an app; other runs
mention it. An app without `.scripts/test/all.sh` is listed as untested,
not failed.

**Adding a dependency repo.** Copy `.github/workflows/catalog-core-dispatch.yaml`
from any dependency repo (it is identical everywhere) and give the repo the
`APP_ID` variable and `PRIVATE_KEY` secret. Repo names follow
`libs/<x>` ↔ `lib-<x>`, `apps/<x>` ↔ `app-<x>`.

**Nightly and weekly.** Every night all apps are tested on `staging`, every
week on `stable` (the cron lines in `catalog-test.yml`). Discord hears
about it only when a configuration starts or stops failing, or when the run
couldn't run at all. A configuration that fails in its first nightly
counts as starting to fail. Flaky results and CI errors don't count as
changes.

**Settings** (repository variables and secrets; all variables optional):

| name | default | |
|---|---|---|
| `CI_TRUSTED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | whose PRs run without approval, and who may `recheck` |
| `CI_MAX_DEP_PRS` | `8` | limit on PRs tested together |
| `DISCORD_WEBHOOK_URL` (secret) | – | nightly notifications; none if unset |
| `DISCORD_MENTION` | – | e.g. `<@&role-id>`: pinged on new failures, and nothing else can ping |
| `APP_ID`, `PRIVATE_KEY` (secret) | – | in catalog-core: the App that writes checks; in dependency repos: the App that starts runs |

**Deploying** (things the workflows rely on):

- Two GitHub Apps, because the dependency repos' key can be used by anyone
  who can push a workflow to any of them:
  - the *dispatch* App, installed on catalog-core only, with `actions:
    write`. Its `APP_ID` and `PRIVATE_KEY` go in every dependency repo.
  - the *checks* App, installed on the dependency repos, with `checks:
    write`. Its `APP_ID` and `PRIVATE_KEY` go in catalog-core only.

  Neither may have `contents: write` on catalog-core. Protect `main` and
  `ci-data` with rulesets; on `ci-data`, allow only GitHub Actions to push.
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
2. **test** (one job per app and compiler, no permissions or secrets): `ukci
   run` runs each variant through the app's own `all.sh`, retries, and runs
   failures without the PRs. Output: `result.json`.
3. **report** (trusted): `ukci report` turns the results into verdicts,
   finishes the check, records the run on the `ci-data` branch, and tells
   Discord after scheduled runs.
4. **fan-out** (trusted): re-tests the PRs that were tested together with
   this one, once (dependents don't fan out further). It runs after the
   report, so a PR that ends up with the same commits reuses this result.
   Dependents that would test the same commits as each other run once: the
   first gets the others in `then` and starts them after its report.

Dependency repos start a run with `workflow_dispatch` (inputs `repo`,
`number`, `comment`, `cascade`, `then`); a maintainer can do the same by
hand from the Actions tab.

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

The tests, linters and their workflow live on the `ci/dev` branch, on top of
this one. Locally:

```sh
cd .github/ci
uv sync
uv run python -m ukci --root ../.. apps
uv run python -m ukci --root ../.. run --plan plan.json --app c-hello --compiler gcc --no-sudo
```

| module | |
|---|---|
| `models.py` | the data passed between jobs (`plan.json`, `result.json`) |
| `apps.py` | apps, their dependencies and variants; repo names |
| `resolve.py`, `plan.py` | Depends-On, then what to test at which commits |
| `run.py` | one app and compiler, variant by variant |
| `verdict.py` | results to verdicts |
| `report.py`, `notify.py`, `render.py` | the check, Discord, what people read |
| `github.py`, `git.py`, `data.py` | `gh`, git, and the `ci-data` branch |
| `dashboard.py` | the Pages dashboard |
