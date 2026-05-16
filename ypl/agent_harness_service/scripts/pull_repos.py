"""Cron script to ensure every configured repo is cloned and up to date.

Does two things on every tick (driven by the ahs-pull-agent-repos systemd
timer, every 5 minutes):

1. Ensures every entry in ``shared_repos.yaml`` is cloned into
   ``AHS_REPOS_DIR`` (default ``/data/ahs/repos/``). This is what gets a
   fresh VM bootstrapped without any hardcoded clone list in
   ``setup_vm.sh``.
2. Pulls every repo currently on disk — both configured entries and any
   ad-hoc clones added via the ``add_shared_repo`` MCP tool.

Exit codes:

- ``0`` — success, or only ad-hoc pull failures (transient repo issues
  shouldn't fail the cron and trigger alerts).
- ``1`` — at least one *configured* entry failed to clone, OR
  ``shared_repos.yaml`` exists but couldn't be parsed. Either condition
  means the VM is in a broken/incomplete state that operators need to
  see in the systemd journal.

Run manually:

    python -m ypl.agent_harness_service.scripts.pull_repos
"""

import sys

from ypl.agent_harness_service.tools.repo_manager import pull_all_repos


def main() -> int:
    result = pull_all_repos()
    pulls: dict[str, bool] = result["pulls"]
    clone_failures: dict[str, str] = result["clone_failures"]
    config_unparseable: bool = result["config_unparseable"]

    for repo, success in pulls.items():
        status = "ok" if success else "FAILED"
        print(f"  pull {repo}: {status}")

    for repo, err in clone_failures.items():
        print(f"  clone {repo}: FAILED ({err})", file=sys.stderr)

    if config_unparseable:
        print(
            "ERROR: shared_repos.yaml exists but is unparseable; "
            "no entries were considered. Fix the yaml and the next tick will heal.",
            file=sys.stderr,
        )

    if clone_failures or config_unparseable:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
