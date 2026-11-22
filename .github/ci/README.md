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

- Several PRs **in the same repo** must be **stacked**: each one built on
  the previous one. CI uses the top one and does not merge PRs together.
- All PRs must target the same branch (`staging` or `stable`).
- Already-merged dependencies are skipped. Closed ones are an error.
- Each PR is tested as GitHub's merge of it into its target branch. If it
  conflicts, rebase.

**Reading the result.**

| | verdict | meaning | blocks? |
|---|---|---|---|
| ❌ | regression | fails with your PRs, passes without them | yes |
| ❌ | failure | fails, nothing to compare to (scheduled runs) | yes |
| ⚠️ | CI infrastructure error | CI broke (download, runner, …); re-run | yes |
| 🟡 | flaky | failed, then passed on retry with the same code | no |
| ⚪ | already broken on base | fails the same way without your PRs | no |
| ✅ | passed | | |

Builds are never retried, since they're deterministic. A failed run is retried once.
Anything that fails is run again on the base branch without your PRs.

**Reproducing locally.**

```sh
./setup.sh                          # clone the dependencies (staging)
cd nginx && ./.scripts/test/all.sh  # what CI runs, all variants
```

## For maintainers

**Approving runs.** PRs from authors who aren't members, owners or
collaborators (including any PR in their `Depends-On` set) only run after
a maintainer approves the `ci-needs-approval` environment in the workflow
run linked from the check. Every new push needs a new approval.

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

`.github/workflows/catalog-test.yml` runs three jobs:

1. **plan** (trusted, never runs PR code): `ukci plan` resolves `Depends-On`,
   pins one commit per repo (`lock`), plus the same commits without the PRs
   (`base_lock`), and picks the apps. → `plan.json`
2. **test** (one job per app and compiler, no permissions or secrets): `ukci run`
   runs each variant through the app's own `all.sh`, then retries and
   re-runs failures on `base_lock`. → `result.json`
3. **report** (trusted): `ukci report` turns results into verdicts and
   finishes the check.

Code: `.github/ci/ukci/`. Tests: `PYTHONPATH=.github/ci python3 -m unittest discover -s .github/ci/tests -t .github/ci`.
