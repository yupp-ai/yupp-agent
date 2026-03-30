#!/usr/bin/env python3
"""
Manual integration test for the AHS BCH proxy binary.

Usage:
    # 1. Build the binary first:
    bash services/command-handler/build.sh

    # 2. Run this script:
    python3 services/command-handler/test_manual.py [path/to/ahs-command-handler]

    # Or with an explicit binary path:
    python3 services/command-handler/test_manual.py ./ahs-command-handler

The script sends requests to the proxy over stdio, collects responses, and
prints a pass/fail summary.  Exit code 0 = all tests passed, 1 = failures.
"""

import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
BOLD = "\033[1m"


def _col(color: str, text: str) -> str:
    return f"{color}{text}{RESET}" if sys.stdout.isatty() else text


class Proxy:
    """Thin wrapper around the BCH proxy subprocess."""

    def __init__(self, binary: str) -> None:
        self.proc = subprocess.Popen(
            [binary],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._responses: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        if self.proc.stdout is None:
            return
        for line in self.proc.stdout:
            try:
                r = json.loads(line)
                with self._lock:
                    self._responses[r["req_id"]] = r
            except Exception:
                pass

    def send(self, req_id: str, tool: str, **args: Any) -> dict[str, Any]:
        """Send a request and block until a response arrives (5 s timeout)."""
        payload = json.dumps({"req_id": req_id, "tool": tool, "args": args}) + "\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(payload.encode())
        self.proc.stdin.flush()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self._lock:
                if req_id in self._responses:
                    return self._responses.pop(req_id)
            time.sleep(0.02)
        raise TimeoutError(f"No response for {req_id} within 5 s")

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except Exception:
            self.proc.kill()


class Suite:
    """Collects test results and prints a summary."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self._current_section = ""

    def section(self, name: str) -> None:
        self._current_section = name
        print(f"\n{BOLD}{name}{RESET}")

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed += 1
            print(f"  {_col(GREEN, '✓')} {name}")
        else:
            self.failed += 1
            print(f"  {_col(RED, '✗')} {name}")
            if detail:
                for line in detail.strip().splitlines():
                    print(f"      {_col(YELLOW, line)}")

    def assert_ok(self, name: str, resp: dict[str, Any], contains: str = "") -> str:
        ok = resp.get("ok") is True
        result = str(resp.get("result", ""))
        if ok and contains and contains not in result:
            ok = False
            detail = f"Expected {contains!r} in result, got: {result!r}"
        else:
            detail = str(resp.get("error", "")) if not ok else ""
        self.check(name, ok, detail)
        return result

    def assert_fail(self, name: str, resp: dict[str, Any], contains: str = "") -> str:
        ok_val = resp.get("ok") is False
        error = str(resp.get("error", ""))
        if ok_val and contains and contains not in error:
            ok_val = False
            detail = f"Expected {contains!r} in error, got: {error!r}"
        else:
            detail = "Expected failure but got ok=True" if not ok_val else ""
        self.check(name, ok_val, detail)
        return error

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{'─' * 50}")
        if self.failed == 0:
            print(_col(GREEN, f"All {total} tests passed ✓"))
        else:
            print(_col(RED, f"{self.failed}/{total} tests FAILED"))
        return 0 if self.failed == 0 else 1


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────


def run_tests(proxy: Proxy, s: Suite, workdir: Path) -> None:
    # ── Bash ──────────────────────────────────────────────────────────────────
    s.section("Bash")

    r = proxy.send("b1", "Bash", command="echo 'hello bch'")
    s.assert_ok("basic echo", r, contains="hello bch")

    r = proxy.send("b2", "Bash", command="printf 'line1\\nline2\\n'")
    result = s.assert_ok("multi-line output", r)
    s.check("two lines present", "line1" in result and "line2" in result)

    r = proxy.send("b3", "Bash", command="exit 42")
    s.assert_fail("non-zero exit code", r, contains="exit code 42")

    r = proxy.send("b4", "Bash", command="echo stdout; echo stderr >&2")
    s.assert_ok("stdout+stderr combined", r, contains="stdout")
    # stderr should also be in the combined output
    s.check("stderr in combined output", "stderr" in r.get("result", ""))

    r = proxy.send("b5", "Bash", command="sleep 10", timeout=0.3)
    s.assert_fail("timeout respected", r, contains="timed out")

    r = proxy.send("b6", "Bash", command="VAR=hello; echo $VAR")
    s.assert_ok("shell variable expansion", r, contains="hello")

    r = proxy.send("b7", "Bash", command="cd /tmp && pwd")
    s.assert_ok("cd works", r, contains="/tmp")

    # ── Read ──────────────────────────────────────────────────────────────────
    s.section("Read")

    sample = workdir / "sample.txt"
    sample.write_text("alpha\nbeta\ngamma\ndelta\nepsilon\n")

    r = proxy.send("rd1", "Read", file_path=str(sample))
    result = s.assert_ok("read full file", r)
    s.check("cat-n format (line numbers)", "     1\talpha" in result)
    s.check("all 5 lines present", "     5\tepsilon" in result)

    r = proxy.send("rd2", "Read", file_path=str(sample), offset=2, limit=2)
    result = s.assert_ok("read with offset+limit", r)
    s.check("starts at line 2", "     2\tbeta" in result)
    s.check("ends at line 3", "     3\tgamma" in result)
    s.check("line 1 not present", "alpha" not in result)
    s.check("line 4 not present", "delta" not in result)

    r = proxy.send("rd3", "Read", file_path=str(sample), offset=4)
    result = s.assert_ok("read from offset to end", r)
    s.check("starts at line 4", "     4\tdelta" in result)

    r = proxy.send("rd4", "Read", file_path="/nonexistent/file.txt")
    s.assert_fail("missing file returns error", r)

    # ── Write ─────────────────────────────────────────────────────────────────
    s.section("Write")

    out_file = workdir / "written.txt"
    r = proxy.send("wr1", "Write", file_path=str(out_file), content="foo\nbar\n")
    s.assert_ok("write creates file", r)
    s.check("file exists on disk", out_file.exists())
    s.check("file content correct", out_file.read_text() == "foo\nbar\n")

    r = proxy.send("wr2", "Write", file_path=str(out_file), content="overwritten\n")
    s.assert_ok("overwrite existing file", r)
    s.check("content overwritten", out_file.read_text() == "overwritten\n")

    nested = workdir / "deep" / "nested" / "dir" / "file.txt"
    r = proxy.send("wr3", "Write", file_path=str(nested), content="nested")
    s.assert_ok("write creates parent dirs", r)
    s.check("nested file exists", nested.exists())

    # ── Edit ──────────────────────────────────────────────────────────────────
    s.section("Edit")

    edit_file = workdir / "edit.txt"
    edit_file.write_text("the quick brown fox\njumps over the lazy dog\n")

    r = proxy.send("ed1", "Edit", file_path=str(edit_file), old_string="quick brown", new_string="slow green")
    s.assert_ok("single replacement", r, contains="Replaced 1")
    s.check("content updated on disk", "slow green" in edit_file.read_text())

    r = proxy.send("ed2", "Edit", file_path=str(edit_file), old_string="DOES_NOT_EXIST", new_string="x")
    s.assert_fail("old_string not found → error", r, contains="not found")

    # Set up multiple occurrences
    edit_file.write_text("cat cat cat\n")
    r = proxy.send("ed3", "Edit", file_path=str(edit_file), old_string="cat", new_string="dog")
    s.assert_fail("non-unique old_string → error (replace_all=false)", r, contains="not unique")

    r = proxy.send("ed4", "Edit", file_path=str(edit_file), old_string="cat", new_string="dog", replace_all=True)
    s.assert_ok("replace_all replaces all occurrences", r, contains="Replaced 3")
    s.check("all occurrences replaced", edit_file.read_text() == "dog dog dog\n")

    r = proxy.send("ed5", "Edit", file_path=str(edit_file), old_string="dog", new_string="dog")
    s.assert_fail("identical old/new_string → error", r, contains="identical")

    # ── Glob ──────────────────────────────────────────────────────────────────
    s.section("Glob")

    # Create a small tree in workdir
    (workdir / "src").mkdir(exist_ok=True)
    (workdir / "src" / "main.py").write_text("# main")
    (workdir / "src" / "util.py").write_text("# util")
    (workdir / "src" / "helper.go").write_text("// helper")
    (workdir / "tests").mkdir(exist_ok=True)
    (workdir / "tests" / "test_main.py").write_text("# test")

    r = proxy.send("gl1", "Glob", pattern="**/*.py", path=str(workdir))
    result = s.assert_ok("glob **/*.py finds py files", r)
    py_paths = [ln for ln in result.splitlines() if ln]
    s.check("found 3 python files", len(py_paths) == 3, f"got: {py_paths}")
    s.check("no .go files in result", not any(".go" in p for p in py_paths))

    r = proxy.send("gl2", "Glob", pattern="*.py", path=str(workdir / "src"))
    result = s.assert_ok("simple *.py pattern", r)
    s.check("2 py files in src/", len([ln for ln in result.splitlines() if ln]) == 2)

    r = proxy.send("gl3", "Glob", pattern="**/*.go", path=str(workdir))
    result = s.assert_ok("glob **/*.go finds go files", r)
    s.check("found exactly 1 .go file", len([ln for ln in result.splitlines() if ln]) == 1)

    r = proxy.send("gl4", "Glob", pattern="**/*.xyz", path=str(workdir))
    result = s.assert_ok("no matches returns empty string", r)
    s.check("empty result for no matches", result.strip() == "")

    # ── Grep ──────────────────────────────────────────────────────────────────
    s.section("Grep  (requires 'rg' / ripgrep on PATH)")

    rg_available = subprocess.run(["which", "rg"], capture_output=True).returncode == 0

    if not rg_available:
        print(f"  {_col(YELLOW, '⚠ rg not found — skipping Grep tests')}")
        print(f"  {_col(YELLOW, '  Install ripgrep: https://github.com/BurntSushi/ripgrep#installation')}")
    else:
        grep_dir = workdir / "grep_src"
        grep_dir.mkdir(exist_ok=True)
        (grep_dir / "alpha.py").write_text("def foo():\n    return 1\n\ndef bar():\n    return 2\n")
        (grep_dir / "beta.py").write_text("class Baz:\n    def foo(self):\n        pass\n")
        (grep_dir / "other.txt").write_text("foo is not a python keyword\n")

        r = proxy.send("gr1", "Grep", pattern="def foo", path=str(grep_dir), output_mode="files_with_matches")
        result = s.assert_ok("files_with_matches mode", r)
        files = [ln for ln in result.splitlines() if ln]
        s.check("2 files matched", len(files) == 2, f"got {files}")

        r = proxy.send("gr2", "Grep", pattern="def foo", path=str(grep_dir), output_mode="content")
        result = s.assert_ok("content mode returns matching lines", r)
        s.check("line numbers present (default -n)", "1:" in result or ":1:" in result)

        r = proxy.send("gr3", "Grep", pattern="def foo", path=str(grep_dir), output_mode="content", **{"-n": False})
        result = s.assert_ok("content mode with -n=false (no line numbers)", r)

        r = proxy.send(
            "gr4", "Grep", pattern="DEF FOO", path=str(grep_dir), output_mode="files_with_matches", **{"-i": True}
        )
        result = s.assert_ok("case-insensitive search", r)
        s.check("still finds 2 files", len([ln for ln in result.splitlines() if ln]) == 2)

        r = proxy.send(
            "gr5", "Grep", pattern="def foo", path=str(grep_dir), glob="*.txt", output_mode="files_with_matches"
        )
        result = s.assert_ok("glob filter *.txt (no py matches)", r)
        s.check("no matches with .txt filter", result.strip() == "")

        r = proxy.send("gr6", "Grep", pattern="def foo", path=str(grep_dir), glob="*.py", output_mode="count")
        result = s.assert_ok("count mode", r)
        s.check("count lines present", len([ln for ln in result.splitlines() if ln]) > 0)

        r = proxy.send("gr7", "Grep", pattern="NOMATCH_XYZ_123", path=str(grep_dir))
        s.assert_ok("no matches returns empty (not error)", r)

        r = proxy.send("gr8", "Grep", pattern="def foo", path=str(grep_dir), output_mode="content", head_limit=1)
        result = s.assert_ok("head_limit caps output", r)
        s.check("only 1 line returned", len([ln for ln in result.splitlines() if ln]) == 1)

    # ── Concurrency ───────────────────────────────────────────────────────────
    s.section("Concurrency  (overlapping requests resolved by req_id)")

    # Fire 5 bash commands that all sleep briefly, in parallel.
    req_ids = [f"con{i}" for i in range(5)]
    assert proxy.proc.stdin is not None
    for rid in req_ids:
        payload = json.dumps({"req_id": rid, "tool": "Bash", "args": {"command": f"sleep 0.1 && echo {rid}"}}) + "\n"
        proxy.proc.stdin.write(payload.encode())
    proxy.proc.stdin.flush()

    results: dict[str, Any] = {}
    deadline = time.monotonic() + 5.0
    while len(results) < len(req_ids) and time.monotonic() < deadline:
        with proxy._lock:
            for rid in req_ids:
                if rid in proxy._responses and rid not in results:
                    results[rid] = proxy._responses.pop(rid)
        time.sleep(0.05)

    s.check(
        f"all {len(req_ids)} concurrent requests answered", len(results) == len(req_ids), f"only got {len(results)}"
    )
    for rid in req_ids:
        if rid in results:
            s.check(f"req_id {rid} matched to correct response", rid in results[rid].get("result", ""))

    # ── Unknown tool ──────────────────────────────────────────────────────────
    s.section("Error handling")

    r = proxy.send("err1", "NonExistentTool", foo="bar")
    s.assert_fail("unknown tool returns error", r, contains="unknown tool")

    r = proxy.send("err2", "Bash", command="")
    s.assert_fail("empty bash command returns error", r, contains="required")

    r = proxy.send("err3", "Read", file_path="")
    s.assert_fail("empty file_path returns error", r, contains="required")

    r = proxy.send("err4", "Glob")  # missing pattern
    s.assert_fail("missing glob pattern returns error", r, contains="required")

    r = proxy.send("err5", "Grep")  # missing pattern
    s.assert_fail("missing grep pattern returns error", r, contains="required")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────


def find_binary() -> str:
    """Resolve the binary path: CLI arg → default build output → PATH."""
    if len(sys.argv) > 1:
        return sys.argv[1]

    # Default build output location
    repo_root = Path(__file__).resolve().parents[2]
    default = repo_root / "ypl" / "agent_harness_service" / "executors" / "bin" / "ahs-command-handler"
    if default.exists():
        return str(default)

    # Fallback: next to this script (useful if you cp the binary here)
    local = Path(__file__).with_name("ahs-command-handler")
    if local.exists():
        return str(local)

    print(_col(RED, "Error: ahs-command-handler binary not found."))
    print("Build it first:")
    print("  bash services/command-handler/build.sh")
    print("Or pass the binary path as an argument:")
    print("  python3 services/command-handler/test_manual.py /path/to/ahs-command-handler")
    sys.exit(1)


def main() -> None:
    binary = find_binary()
    print(f"{BOLD}AHS BCH Proxy — Manual Integration Test{RESET}")
    print(f"Binary: {binary}")

    proxy = Proxy(binary)
    s = Suite()

    with tempfile.TemporaryDirectory(prefix="bch_test_") as tmpdir:
        workdir = Path(tmpdir)
        try:
            run_tests(proxy, s, workdir)
        finally:
            proxy.close()

    sys.exit(s.summary())


if __name__ == "__main__":
    main()
