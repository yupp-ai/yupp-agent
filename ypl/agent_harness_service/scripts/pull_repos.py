"""Cron script to auto-pull all repos.

Pulls latest main for all repos in AHS_REPOS_DIR.
Intended to run every 5 minutes via cron:

    */5 * * * * python -m ypl.agent_harness_service.scripts.pull_repos
"""

from ypl.agent_harness_service.tools.repo_manager import pull_all_repos


def main() -> None:
    results = pull_all_repos()
    for repo, success in results.items():
        status = "ok" if success else "FAILED"
        print(f"  {repo}: {status}")


if __name__ == "__main__":
    main()
