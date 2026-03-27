#!/usr/bin/env python3
"""
Setup gh CLI for remote Claude Code sessions.
Translated from https://github.com/oikon48/gh-setup-hooks/blob/main/src/index.js
And added logic to early exit if GITHUB_TOKEN environment variable is not set or if
gh is already available in PATH.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile

LOG_PREFIX = "[gh-setup-hooks]"
LOCAL_BIN = os.path.expanduser("~/.local/bin")
GH_PATH = os.path.join(LOCAL_BIN, "gh")
DEFAULT_GH_VERSION = "2.83.2"

ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def log(msg: str) -> None:
    print(f"{LOG_PREFIX} {msg}", file=sys.stderr)


def update_path() -> None:
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if env_file:
        with open(env_file, "a") as f:
            f.write(f'export PATH="{LOCAL_BIN}:$PATH"\n')
        log("PATH persisted to CLAUDE_ENV_FILE")


def run() -> None:
    # Only run in remote Claude Code environment
    if os.environ.get("CLAUDE_CODE_REMOTE") != "true":
        log("Not a remote session, skipping")
        sys.exit(0)

    # Check if gh is already available in PATH
    try:
        result = subprocess.run(
            ["gh", "--version"],
            capture_output=True,
            text=True,
            check=True,
        )
        version = result.stdout.split("\n")[0]
        log(f"gh CLI already available: {version}")
        sys.exit(0)
    except (subprocess.CalledProcessError, FileNotFoundError):
        # gh not found, continue with installation
        pass

    # Check if gh exists in local bin
    if os.path.exists(GH_PATH):
        log(f"gh found in {LOCAL_BIN}")
        update_path()
        sys.exit(0)

    # Check if GITHUB_TOKEN is set (required for gh auth)
    if not os.environ.get("GITHUB_TOKEN"):
        log("GITHUB_TOKEN not set, skipping gh CLI installation")
        sys.exit(0)

    log(f"Installing gh CLI to {LOCAL_BIN}...")

    # Create local bin directory
    os.makedirs(LOCAL_BIN, exist_ok=True)

    # Detect architecture
    machine = platform.machine().lower()
    arch = ARCH_MAP.get(machine)
    if not arch:
        log(f"Unsupported architecture: {machine}")
        sys.exit(0)

    gh_version = os.environ.get("GH_SETUP_VERSION", DEFAULT_GH_VERSION)
    tarball = f"gh_{gh_version}_linux_{arch}.tar.gz"
    download_url = f"https://github.com/cli/cli/releases/download/v{gh_version}/{tarball}"
    checksum_url = f"https://github.com/cli/cli/releases/download/v{gh_version}/gh_{gh_version}_checksums.txt"

    log(f"Downloading gh v{gh_version} for {arch}...")

    temp_dir = tempfile.mkdtemp(prefix="gh-setup-")

    try:
        tarball_path = os.path.join(temp_dir, tarball)
        checksums_path = os.path.join(temp_dir, "checksums.txt")

        # Download tarball
        subprocess.run(
            [
                "curl",
                "-fsSL",
                "--proto",
                "=https",
                "--tlsv1.2",
                "--connect-timeout",
                "5",
                "--max-time",
                "60",
                download_url,
                "-o",
                tarball_path,
            ],
            check=True,
        )

        # Download and verify checksum
        log("Verifying checksum...")
        try:
            subprocess.run(
                [
                    "curl",
                    "-fsSL",
                    "--connect-timeout",
                    "5",
                    "--max-time",
                    "30",
                    checksum_url,
                    "-o",
                    checksums_path,
                ],
                capture_output=True,
                check=True,
            )
            # Use separate grep and sha256sum calls to avoid shell=True
            grep_result = subprocess.run(
                ["grep", tarball, "checksums.txt"],
                cwd=temp_dir,
                capture_output=True,
                text=True,
                check=True,
            )
            subprocess.run(
                ["sha256sum", "-c", "-"],
                input=grep_result.stdout,
                text=True,
                cwd=temp_dir,
                capture_output=True,
                check=True,
            )
            log("Checksum verified")
        except subprocess.CalledProcessError:
            log("Checksum verification failed. Aborting installation.")
            raise

        # Extract
        log("Extracting...")
        subprocess.run(
            ["tar", "-xzf", tarball_path, "-C", temp_dir],
            capture_output=True,
            check=True,
        )

        # Move binary
        extracted_bin = os.path.join(temp_dir, f"gh_{gh_version}_linux_{arch}", "bin", "gh")
        shutil.copy2(extracted_bin, GH_PATH)
        os.chmod(GH_PATH, 0o755)

        update_path()

        result = subprocess.run([GH_PATH, "--version"], capture_output=True, text=True, check=True)
        version = result.stdout.split("\n")[0]
        log(f"gh CLI installed successfully: {version}")

    except Exception as e:
        log(f"Failed to install gh CLI: {e}")
        sys.exit(1)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Configure git remote to use HTTPS for gh auth compatibility
    remote_url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GH_SETUP_REMOTE_URL")
    if remote_url:
        try:
            subprocess.run(
                ["git", "remote", "set-url", "origin", remote_url],
                capture_output=True,
                check=True,
            )
            log(f"Git remote origin set to {remote_url}")
        except subprocess.CalledProcessError as e:
            log(f"Failed to configure git remote: {e}")
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    run()
