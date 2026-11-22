# catalog-core CI

Pull requests to Unikraft and its libraries (`unikraft`, `lib-*`, `app-*`)
are built and run on the apps in this repository. The result shows up on
the PR as the **Catalog Core** check.

## For contributors

**What gets tested.** Every app whose `setup.sh` links a repository your PR
changes, with gcc and clang, on every variant its `.scripts/test/all.sh`
lists. A `unikraft` PR tests all apps. The check lists which apps ran and why.

**PRs that need each other.** Add a line to the PR description (or to a
commit message):

```
Depends-On: unikraft/lib-musl#12
Depends-On: lib-lwip#40, #7
Depends-On: https://github.com/unikraft/unikraft/pull/1745
```

`repo#N` is a repo in the same organization, `#N` is the same repo. Only
lines starting with `Depends-On:` count. Words like "needs" or "requires"
are ignored. Dependencies are followed transitively, and PRs that depend on
each other (A ↔ B) are fine.

- Several PRs **in the same repo** are fine. If they are stacked (each
  built on the previous one), CI uses the top one. Otherwise CI merges them
  together onto the target branch. If they conflict, CI can't resolve that:
  the check names the PRs and files, and you rebase one onto the other.
- All PRs must target the same branch (`staging` or `stable`).
- Already-merged dependencies are skipped. Closed ones are an error.
- Each PR is tested as GitHub's merge of it into its target branch. If it
  conflicts, rebase.

**Reading the result.** A *configuration* is one app + compiler + variant
(for example nginx, gcc, `qemu.x86_64`). The check shows one block per
distinct failure, with the first error line, and the configurations it hits.

| | verdict | meaning | blocks? |
|---|---|---|---|
| ❌ | regression | passes without your PRs, or fails *differently* without them | yes |
| ❌ | failure | fails, and there is nothing to compare it to (scheduled runs) | yes |
| ⚠️ | CI error | CI couldn't tell: a download failed, a job died, the comparison run is missing. Re-run; it's not your fault | yes |
| 🟡 | flaky | the run failed, then passed on retry with the same code | no |
| ⚪ | already broken | fails the same way without your PRs (same phase, same first error) | no |
| ✅ | passed | | |

Every failure is run once more without your PRs. A failed *run* is also
retried once on the same code. A failed *build* isn't retried, since builds are
deterministic, except that download failures count as CI errors. A run
replaced by a newer push is marked cancelled and isn't counted.

**Re-running.** Comment `recheck` on the PR (members and the PR's author).

**Reproducing locally.**

```sh
./setup.sh                          # clone the dependencies (staging)
cd nginx && ./.scripts/test/all.sh  # what CI runs, all variants
```

## For maintainers

**Approving runs.** PRs from authors who aren't members, owners or
collaborators (including any PR in their `Depends-On` set) only run after
a maintainer approves the `ci-needs-approval` environment in the workflow
run linked from the check. Every new push needs a new approval. Declining
shows up on the PR as "⛔ Not run: @maintainer declined to run this code".

**Adding an app.** Add its directory. CI reads its dependencies from
`setup.sh` (`check_exists_and_create_symlink "<path>"`) and its variants
from `.scripts/test/all.sh` (`test_build <variant>` / `test_build_run
<variant>`). The `lint` workflow fails if it can't parse either. An app
without `.scripts/test/all.sh` is listed as untested, not failed.

**Adding a dependency repo.** Copy `.github/workflows/catalog-core-dispatch.yaml`
from any dependency repo (it is identical everywhere) and give the repo the
`APP_ID` variable and `PRIVATE_KEY` secret. Repo names follow
`libs/<x>` ↔ `lib-<x>`, `apps/<x>` ↔ `app-<x>`.

**Settings** (repository variables, all optional):

| variable | default | |
|---|---|---|
| `CI_TRUSTED_ASSOCIATIONS` | `OWNER,MEMBER,COLLABORATOR` | whose PRs run without approval |
| `CI_MAX_DEP_PRS` | `8` | limit on PRs tested together |

## How it works

`.github/workflows/catalog-test.yml` runs these jobs:

1. **plan** (trusted, never runs PR code): `ukci plan` resolves `Depends-On`,
   pins one commit per repo (`lock`) and the same without the PRs
   (`base_lock`), and picks the apps. Output: `plan.json`. Then `ukci check-start`
   puts the check on the PR.
2. **fan-out** (trusted): re-tests the PRs that were tested together with this one.
3. **test** (one job per app and compiler, no permissions or secrets): `ukci run`
   runs each variant through the app's own `all.sh`, retries a failed run, and
   runs failures again without the PRs. Output: `result.json`.
4. **report** (trusted): `ukci report` turns the results into verdicts, finishes
   the check, records the run on the `ci-data` branch, and posts to Discord
   after scheduled runs if something changed.

## Developing

```sh
cd .github/ci
uv sync                                    # Python and dependencies, from uv.lock
uv run pytest                              # tests
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run python -m ukci --root ../.. apps    # what CI reads from the app directories
```

| module | job |
|---|---|
| `models.py` | the data passed between jobs (`plan.json`, `result.json`) |
| `resolve.py`, `plan.py` | Depends-On, then what to test at which commits |
| `run.py` | one app and compiler, variant by variant |
| `verdict.py` | results, then verdicts |
| `checks.py`, `report.py`, `notify.py` | the check, the job commands, Discord |
| `render.py`, `templates/` | everything people read |
| `github.py`, `git.py`, `data.py` | `gh`, git, and the `ci-data` branch |
