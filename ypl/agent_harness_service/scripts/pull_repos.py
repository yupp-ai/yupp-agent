"""Cron script to ensure every configured repo is cloned and up to date.

Does two things on every tick (driven by the ahs-pull-agent-repos systemd
timer, every 5 minutes):

1. Ensures every entry in ``shared_repos.yaml`` is cloned into
   ``AHS_REPOS_DIR`` (default ``/data/ahs/repos/``). This is what gets a
   fresh VM bootstrapped without any hardcoded clone list in
   ``setup_vm.sh``.
2. Pulls every repo currently on disk — both configured entries and any
   ad-hoc clones added via the ``add_shared_repo`` MCP tool.

Run manually:

    python -m ypl.agent_harness_service.scripts.pull_repos
"""

from ypl.agent_harness_service.tools.repo_manager import pull_all_repos


def main() -> None:
    results = pull_all_repos()
    for repo, success in results.items():
        status = "ok" if success else "FAILED"
        print(f"  {repo}: {status}")


if __name__ == "__main__":
    main()
