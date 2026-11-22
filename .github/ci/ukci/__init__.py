"""catalog-core CI: discover apps, resolve PR dependencies, plan, run, report.

Everything is derived from the app directories and the GitHub API; there
are no config files listing apps, repos or modes.

Run as:  PYTHONPATH=.github/ci python3 -m ukci <command> --help
"""
