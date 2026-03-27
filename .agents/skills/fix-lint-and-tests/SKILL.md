---
name: fix-lint-and-tests
description: Run lint tools (ruff format, ruff check, mypy) and fix issues on the current branch or entire Graphite stack. Also fixes failed tests from CI. Use "stack" argument to fix all PRs in the stack.
---

# Fix Lint and Tests

Run lint tools, fix lint issues, and fix failed tests from CI on a single PR or the entire branch stack.

## Usage

```
/fix-lint-and-tests           # Fix lint and tests on current branch only
/fix-lint-and-tests stack     # Fix lint and tests on entire stack
```

## Tool Selection: Graphite vs Git

This skill uses Graphite (`gt`) by default. If Graphite is not installed, use the equivalent git commands:

| Graphite | Git Equivalent |
|----------|----------------|
| `gt checkout <branch>` | `git checkout <branch>` |
| `gt submit --publish --no-edit --no-interactive` | `git push --force-with-lease` (then manage PR via UI/CLI) |
| `gt restack --no-interactive` | `git rebase main` (then rebase dependent branches manually) |
| `gt log short` | No direct equivalent; manually list branches in the stack |
| `gt continue` | `git rebase --continue` |

## Lint Tools

**Important**: Always activate the `ys-dev` conda environment before running lint tools. Use `conda activate ys-dev`. If this command fails, you may need to initialize conda for your shell first (e.g., by running `conda init bash`).

Run these commands in sequence:
1. `poetry run ruff format <files>`
2. `poetry run ruff check <files> --output-format=github --fix`
3. `poetry run mypy --config-file=pyproject.toml <files>`

## Single Branch Mode (default)

### Steps

1. **Get the PR number for the current branch**:
   ```bash
   gh pr list --head $(git branch --show-current) --json number --jq '.[0].number'
   ```

2. **Check CI status** to identify what needs fixing:
   ```bash
   gh pr checks <PR_NUMBER>
   ```

   Look for failed checks - common names include:
   - Lint: `lint`, `Lint`, `ruff`, `pre-commit`
   - Type check: `mypy`, `type-check`
   - Tests: `test`, `Test`, `tests`, `pytest`, `ci`, `CI`

3. **Identify changed files** in the current branch:
   ```bash
   git diff --name-only HEAD~1 -- '*.py'
   ```

4. **Run lint tools** on the changed Python files (ensure `ys-dev` conda env is active):
   ```bash
   poetry run ruff format <files>
   poetry run ruff check <files> --output-format=github --fix
   poetry run mypy --config-file=pyproject.toml <files>
   ```

5. **If there are mypy errors**, fix them manually

6. **If tests are failing**, fix them (see "Fixing Failed Tests" section below)

7. **Amend the commit** if there are changes:
   ```bash
   git add <files> && git commit --amend --no-edit
   ```

8. **Submit the PR**:
   ```bash
   gt submit --publish --no-edit --no-interactive
   # Or with git: git push --force-with-lease
   ```

## Stack Mode (`/fix-lint-and-tests stack`)

### Steps

1. **Get the list of branches and their PR numbers in the stack**:
   ```bash
   gt log short
   ```

   Then for each branch, get the PR number:
   ```bash
   gh pr list --head <branch-name> --json number --jq '.[0].number'
   ```

2. **Check CI status for each PR** to identify which ones need fixing:
   ```bash
   gh pr checks <PR_NUMBER> --json name,state,conclusion --jq '.[] | select(.name | test("lint|test|mypy|ruff"; "i"))'
   ```

   Or check all checks at once:
   ```bash
   gh pr checks <PR_NUMBER>
   ```

   Look for failed checks with conclusions like "failure" or "cancelled".

3. **Only fix PRs with failed CI** - For each branch with failed lint/test checks (from bottom to top):
   a. Checkout the branch:
      ```bash
      gt checkout <branch-name>
      # Or with git: git checkout <branch-name>
      ```
   b. Run lint tools on changed files
   c. Fix any mypy errors
   d. If tests are failing, fetch logs and fix (see "Fixing Failed Tests" section)
   e. Amend the commit if there are changes
   f. Continue to the next branch

4. **Restack** to propagate changes:
   ```bash
   gt restack --no-interactive
   # Or with git: git rebase main (then rebase dependent branches onto each other)
   ```
   - If there are merge conflicts, resolve them and run `gt continue` (or `git rebase --continue`)

5. **Submit the entire stack**:
   ```bash
   gt submit --publish --no-edit --no-interactive
   # Or with git: git push --force-with-lease (for each branch)
   ```

### Example: Check CI Status for Stack

```bash
# Get all PRs in the stack with their status
for branch in $(gt log short --json | jq -r '.[].name'); do
  pr_num=$(gh pr list --head "$branch" --json number --jq '.[0].number' 2>/dev/null)
  if [ -n "$pr_num" ]; then
    echo "=== PR #$pr_num ($branch) ==="
    gh pr checks "$pr_num" --json name,conclusion --jq '.[] | "\(.name): \(.conclusion)"' | grep -iE "lint|test|mypy|ruff|pytest|ci" || echo "No lint/test checks found"
  fi
done
```

## Fixing Failed Tests

### 1. Fetch Failed Test Output from CI

Get the failed test logs from the GitHub Actions run:

```bash
# Get the failed check's run ID
gh pr checks <PR_NUMBER> --json name,link --jq '.[] | select(.name | test("test"; "i")) | .link'
```

Then fetch the logs:
```bash
# Extract run ID from the URL (e.g., https://github.com/owner/repo/actions/runs/12345678)
gh run view <RUN_ID> --log-failed
```

Alternatively, view the full log:
```bash
gh run view <RUN_ID> --log
```

### 2. Identify the Failing Tests

From the CI logs, look for:
- Test file paths (e.g., `tests/test_foo.py::test_bar`)
- Error messages and stack traces
- Assertion failures

### 3. Run Tests Locally to Reproduce

Ensure the `ys-dev` conda environment is active, then:

```bash
# Run the specific failing test
poetry run pytest <test_file>::<test_name> -v

# Or run all tests in a file
poetry run pytest <test_file> -v

# Run with more debug output
poetry run pytest <test_file>::<test_name> -v --tb=long
```

### 4. Fix the Test or Code

Common test failure patterns:
- **Import errors**: Missing or renamed module/function
- **Assertion failures**: Expected vs actual values don't match
- **Type errors**: Incorrect types passed to functions
- **Mock issues**: Mocks not set up correctly for new code paths
- **Missing fixtures**: Test depends on fixtures not available

After identifying the issue:
1. Read the relevant test file and source code
2. Understand what the test is checking
3. Fix either the test (if the test is wrong) or the source code (if the implementation is wrong)
4. Re-run the test locally to verify the fix

### 5. Verify All Related Tests Pass

Ensure the `ys-dev` conda environment is active, then:

```bash
# Run tests for the affected module
poetry run pytest tests/<module_name>/ -v

# Or run the full test suite if changes are significant
poetry run pytest tests/ -v
```

## CI Check Names to Look For

Common check names that indicate lint/test failures:
- `lint` / `Lint`
- `test` / `Test` / `tests` / `pytest`
- `mypy` / `type-check`
- `ruff`
- `pre-commit`
- `ci` / `CI`
- `unit-tests` / `integration-tests`

## Finding Changed Files

For the current branch, find Python files that were changed:
```bash
# Files changed in the last commit
git diff --name-only HEAD~1 -- '*.py'

# Or all Python files in a specific directory
git diff --name-only HEAD~1 -- 'ypl/**/*.py'
```

## Handling Merge Conflicts During Restack

When restacking after lint fixes, conflicts may occur. Common resolution:

1. Check conflict markers:
   ```bash
   grep -n "<<<<<<\|======\|>>>>>>" <file>
   ```

2. Edit the file to resolve conflicts (usually keep both sets of imports/changes)

3. Mark resolved and continue:
   ```bash
   git add -A && gt continue
   # Or with git: git add -A && git rebase --continue
   ```

## Important Notes

- Always run mypy after ruff - ruff fixes formatting but doesn't catch type errors
- If mypy finds errors, investigate and fix them before amending
- When fixing stack, work from bottom branch to top to minimize conflicts
- After restacking, verify lint still passes on each branch
- For test failures, always reproduce locally before attempting to fix
- If a test failure is flaky (passes locally but fails in CI), check for race conditions or environment-specific issues
