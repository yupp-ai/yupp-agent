package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// ──────────────────────────────────────────────────────────────────────────────
// Bash
// ──────────────────────────────────────────────────────────────────────────────

const defaultBashTimeoutSeconds = 120.0

// allowedEnvPrefixes lists the env-var prefixes that are safe to forward to
// bash subprocesses.  Credentials, service tokens, and cloud-provider vars are
// intentionally excluded to avoid leaking them via `env`/`printenv`.
var allowedEnvPrefixes = []string{
	"PATH=", "HOME=", "USER=", "LOGNAME=", "SHELL=",
	"TMPDIR=", "TMP=", "TEMP=",
	"LANG=", "LC_", "TERM=", "COLORTERM=",
	"PWD=", "OLDPWD=",
}

// buildAllowedEnv returns a copy of os.Environ filtered to allowedEnvPrefixes.
// HOME and PWD are pinned to the current working directory to prevent child
// processes from reading host dotfiles (~/.ssh, ~/.aws, etc.).
func buildAllowedEnv() []string {
	cwd, err := os.Getwd()
	if err != nil {
		cwd = "/tmp"
	}

	all := os.Environ()
	allowed := make([]string, 0, len(all))
	for _, kv := range all {
		// Skip HOME and PWD — we override them below.
		if strings.HasPrefix(kv, "HOME=") || strings.HasPrefix(kv, "PWD=") {
			continue
		}
		for _, prefix := range allowedEnvPrefixes {
			if strings.HasPrefix(kv, prefix) {
				allowed = append(allowed, kv)
				break
			}
		}
	}
	allowed = append(allowed, "HOME="+cwd, "PWD="+cwd)
	return allowed
}

// BashArgs are the arguments for the Bash tool.
type BashArgs struct {
	Command string  `json:"command"`
	Timeout float64 `json:"timeout"` // seconds; 0 → use default
}

// handleBash runs a shell command and returns the combined stdout+stderr.
// A non-zero exit code is surfaced as an error carrying the output.
//
// Design note on process-group kill:
//   exec.CommandContext kills only the direct child (sh), but sh may have
//   spawned grandchildren (e.g. "sleep 10") that inherit the stdout/stderr
//   pipe.  If we only kill sh, those grandchildren keep the pipe open and
//   cmd.Wait() blocks forever.  By placing the shell in its own process
//   group (Setpgid) and sending SIGKILL to the whole group on timeout, every
//   descendant is killed, all pipe fds are released, and Wait() returns.
func handleBash(ctx context.Context, raw json.RawMessage) (string, error) {
	var args BashArgs
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", fmt.Errorf("bash: invalid args: %w", err)
	}
	if args.Command == "" {
		return "", fmt.Errorf("bash: command is required")
	}

	// Fast-fail if the session is already shutting down.
	if ctx.Err() != nil {
		return "", fmt.Errorf("bash: cancelled before start")
	}

	timeout := args.Timeout
	if timeout <= 0 {
		timeout = defaultBashTimeoutSeconds
	}

	cmdCtx, cancel := context.WithTimeout(ctx, time.Duration(timeout*float64(time.Second)))
	defer cancel()

	cmd := exec.Command("sh", "-c", args.Command) //nolint:gosec
	// New process group so we can SIGKILL all descendants at once.
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	// Restrict child environment to safe prefixes only — prevents leaking
	// secrets (API keys, DB credentials, cloud tokens) via env/printenv.
	cmd.Env = buildAllowedEnv()

	// Build a merged stdout+stderr pipe manually so we control when to close
	// the write end.  We close pw in the parent immediately after Start so
	// that io.ReadAll returns EOF once all writers (sh + its children) exit.
	pr, pw, err := os.Pipe()
	if err != nil {
		return "", fmt.Errorf("bash: pipe: %w", err)
	}
	cmd.Stdout = pw
	cmd.Stderr = pw

	if err := cmd.Start(); err != nil {
		pw.Close()
		pr.Close()
		return "", fmt.Errorf("bash: start: %w", err)
	}
	pw.Close() // parent no longer writes; EOF once all children die

	// Drain output in a goroutine — unblocks as soon as all pipe writers close.
	// Cap retained bytes at 10 MiB; drain (discard) the remainder so the child
	// never gets SIGPIPE from a full pipe after the cap is hit.
	const maxOutputBytes = 10 * 1024 * 1024
	outCh := make(chan []byte, 1)
	go func() {
		data, _ := io.ReadAll(io.LimitReader(pr, maxOutputBytes))
		_, _ = io.Copy(io.Discard, pr) // drain remainder; unblocks any still-writing children
		pr.Close()
		outCh <- data
	}()

	// Wait for process exit.
	waitCh := make(chan error, 1)
	go func() { waitCh <- cmd.Wait() }()

	killGroup := func() {
		if cmd.Process != nil {
			_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		}
	}

	select {
	case waitErr := <-waitCh:
		// Kill the process group before draining output. sh may have exited
		// but backgrounded grandchildren (e.g. "sleep 300 &") still hold the
		// write-end of the pipe open, causing <-outCh to block indefinitely.
		// SIGKILL to the whole group releases all pipe fds immediately.
		killGroup()
		output := string(<-outCh)

		if waitErr == nil {
			return output, nil
		}
		// Check if parent context was cancelled (SIGTERM / stdin EOF).
		if cmd.ProcessState != nil && cmd.ProcessState.ExitCode() == -1 && ctx.Err() != nil {
			return "", fmt.Errorf("bash: cancelled\n%s", output)
		}
		exitCode := -1
		if cmd.ProcessState != nil {
			exitCode = cmd.ProcessState.ExitCode()
		}
		return "", fmt.Errorf("bash: exit code %d\n%s", exitCode, output)

	case <-cmdCtx.Done():
		// Timeout or parent context cancelled — kill the entire process group.
		killGroup()
		<-waitCh          // ensure sh has exited
		output := string(<-outCh) // drain remaining output

		if cmdCtx.Err() == context.DeadlineExceeded {
			return "", fmt.Errorf("bash: timed out after %gs\n%s", timeout, output)
		}
		return "", fmt.Errorf("bash: cancelled\n%s", output)
	}
}

// ──────────────────────────────────────────────────────────────────────────────
// Read
// ──────────────────────────────────────────────────────────────────────────────

// ReadArgs are the arguments for the Read tool.
type ReadArgs struct {
	FilePath string `json:"file_path"`
	// Offset is the 1-based line number to start reading from.
	// 0 (or absent) means "start from line 1".
	Offset int `json:"offset"`
	// Limit is the maximum number of lines to return.
	// 0 (or absent) means "return all lines".
	Limit int `json:"limit"`
}

// handleRead reads a text file and returns its contents in cat-n format:
//
//	"     1\tline one\n     2\tline two\n..."
//
// The original line numbers are always preserved in the output.
func handleRead(_ context.Context, raw json.RawMessage) (string, error) {
	var args ReadArgs
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", fmt.Errorf("read: invalid args: %w", err)
	}
	if args.FilePath == "" {
		return "", fmt.Errorf("read: file_path is required")
	}

	// Guard against reading multi-GB log/data files fully into memory.
	// The offset/limit args bound output but the full file is loaded first —
	// cap at maxReadBytes and truncate at a line boundary like handleRead does.
	const maxReadBytes = 5 * 1024 * 1024 // 5 MiB

	f, err := os.Open(args.FilePath)
	if err != nil {
		return "", fmt.Errorf("read: %w", err)
	}
	defer f.Close() //nolint:errcheck

	limited := io.LimitReader(f, int64(maxReadBytes+1))
	buf, err := io.ReadAll(limited)
	if err != nil {
		return "", fmt.Errorf("read: %w", err)
	}
	truncated := len(buf) > maxReadBytes
	if truncated {
		buf = buf[:maxReadBytes]
		if idx := strings.LastIndexByte(string(buf), '\n'); idx >= 0 {
			buf = buf[:idx+1]
		}
	}
	data := buf

	// Split into lines preserving the original content.
	rawLines := strings.Split(string(data), "\n")
	// Remove trailing empty element from final newline.
	if len(rawLines) > 0 && rawLines[len(rawLines)-1] == "" {
		rawLines = rawLines[:len(rawLines)-1]
	}

	var sb strings.Builder
	linesWritten := 0

	for i, line := range rawLines {
		lineNum := i + 1 // 1-based

		// Skip lines before the requested offset.
		if args.Offset > 0 && lineNum < args.Offset {
			continue
		}
		// Stop once we've written the requested limit.
		if args.Limit > 0 && linesWritten >= args.Limit {
			break
		}

		// cat -n format: right-aligned 6-char line number, tab, content.
		fmt.Fprintf(&sb, "%6d\t%s\n", lineNum, line)
		linesWritten++
	}

	if truncated {
		fmt.Fprintf(&sb, "\n[... file truncated at %d MiB ...]\n", maxReadBytes/(1024*1024))
	}

	return sb.String(), nil
}

// ──────────────────────────────────────────────────────────────────────────────
// Write
// ──────────────────────────────────────────────────────────────────────────────

// WriteArgs are the arguments for the Write tool.
type WriteArgs struct {
	FilePath string `json:"file_path"`
	Content  string `json:"content"`
}

// handleWrite writes (or overwrites) a file, creating parent directories as
// needed.
func handleWrite(_ context.Context, raw json.RawMessage) (string, error) {
	var args WriteArgs
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", fmt.Errorf("write: invalid args: %w", err)
	}
	if args.FilePath == "" {
		return "", fmt.Errorf("write: file_path is required")
	}

	if err := os.MkdirAll(filepath.Dir(args.FilePath), 0o755); err != nil {
		return "", fmt.Errorf("write: failed to create parent directories: %w", err)
	}

	// Preserve existing file permissions; fall back to 0o644 for new files.
	perm := os.FileMode(0o644)
	if info, err := os.Stat(args.FilePath); err == nil {
		perm = info.Mode().Perm()
	}

	if err := os.WriteFile(args.FilePath, []byte(args.Content), perm); err != nil {
		return "", fmt.Errorf("write: %w", err)
	}

	return fmt.Sprintf("Wrote %d bytes to %s", len(args.Content), args.FilePath), nil
}

// ──────────────────────────────────────────────────────────────────────────────
// Edit
// ──────────────────────────────────────────────────────────────────────────────

// EditArgs are the arguments for the Edit tool.
type EditArgs struct {
	FilePath   string `json:"file_path"`
	OldString  string `json:"old_string"`
	NewString  string `json:"new_string"`
	ReplaceAll bool   `json:"replace_all"`
}

// handleEdit performs an exact-string replacement in a file.
//
// When replace_all is false (default), the old_string must appear exactly once;
// if it appears zero or more than once the edit is rejected.
// When replace_all is true, every occurrence is replaced.
func handleEdit(_ context.Context, raw json.RawMessage) (string, error) {
	var args EditArgs
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", fmt.Errorf("edit: invalid args: %w", err)
	}
	if args.FilePath == "" {
		return "", fmt.Errorf("edit: file_path is required")
	}
	if args.OldString == "" {
		return "", fmt.Errorf("edit: old_string is required")
	}
	if args.OldString == args.NewString {
		return "", fmt.Errorf("edit: old_string and new_string are identical — nothing to change")
	}

	// Stat first: check size and capture permissions before reading.
	// Editing huge files (log dumps, binaries) would load them entirely into
	// memory — return a clear error instead of risking an OOM.
	const maxEditBytes = 10 * 1024 * 1024 // 10 MiB
	info, err := os.Stat(args.FilePath)
	if err != nil {
		return "", fmt.Errorf("edit: %w", err)
	}
	if info.Size() > maxEditBytes {
		return "", fmt.Errorf("edit: file %q is %d bytes — exceeds the %d MiB limit for in-memory editing",
			args.FilePath, info.Size(), maxEditBytes/(1024*1024))
	}
	perm := info.Mode().Perm()

	data, err := os.ReadFile(args.FilePath)
	if err != nil {
		return "", fmt.Errorf("edit: %w", err)
	}

	content := string(data)
	count := strings.Count(content, args.OldString)

	if count == 0 {
		return "", fmt.Errorf("edit: old_string not found in %s", args.FilePath)
	}

	var newContent string
	var replaced int

	if args.ReplaceAll {
		newContent = strings.ReplaceAll(content, args.OldString, args.NewString)
		replaced = count
	} else {
		if count > 1 {
			return "", fmt.Errorf(
				"edit: old_string is not unique in %s (%d occurrences); "+
					"use replace_all:true or add more surrounding context to make it unique",
				args.FilePath, count,
			)
		}
		newContent = strings.Replace(content, args.OldString, args.NewString, 1)
		replaced = 1
	}

	if err := os.WriteFile(args.FilePath, []byte(newContent), perm); err != nil {
		return "", fmt.Errorf("edit: failed to write %s: %w", args.FilePath, err)
	}

	noun := "occurrence"
	if replaced != 1 {
		noun = "occurrences"
	}
	return fmt.Sprintf("Replaced %d %s in %s", replaced, noun, args.FilePath), nil
}

// ──────────────────────────────────────────────────────────────────────────────
// Glob
// ──────────────────────────────────────────────────────────────────────────────

// GlobArgs are the arguments for the Glob tool.
type GlobArgs struct {
	Pattern string `json:"pattern"`
	// Path is the base directory to search.  Defaults to ".".
	Path string `json:"path"`
}

// fileEntry holds a matched path and its modification time for sorting.
type fileEntry struct {
	path    string
	modTime time.Time
}

// handleGlob finds all filesystem entries matching a glob pattern (** supported)
// and returns their paths sorted by modification time (most recent first),
// one path per line.
func handleGlob(ctx context.Context, raw json.RawMessage) (string, error) {
	var args GlobArgs
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", fmt.Errorf("glob: invalid args: %w", err)
	}
	if args.Pattern == "" {
		return "", fmt.Errorf("glob: pattern is required")
	}

	basePath := args.Path
	if basePath == "" {
		basePath = "."
	}

	absBase, err := filepath.Abs(basePath)
	if err != nil {
		return "", fmt.Errorf("glob: invalid path %q: %w", basePath, err)
	}

	// Cap results to prevent unbounded memory growth from broad patterns like **/*.
	const maxGlobResults = 10_000

	var matches []fileEntry

	if strings.Contains(args.Pattern, "**") {
		// Walk the tree and match each relative path against the pattern.
		err = filepath.WalkDir(absBase, func(p string, d os.DirEntry, walkErr error) error {
			if walkErr != nil {
				return nil // skip unreadable entries
			}
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if len(matches) >= maxGlobResults {
				return filepath.SkipAll
			}

			rel, err := filepath.Rel(absBase, p)
			if err != nil || rel == "." {
				return nil
			}

			// Normalise separators for matching.
			if matchDoubleStarGlob(args.Pattern, rel) {
				info, err := d.Info()
				if err != nil {
					return nil
				}
				matches = append(matches, fileEntry{path: p, modTime: info.ModTime()})
			}
			return nil
		})
		if err != nil && ctx.Err() == nil {
			return "", fmt.Errorf("glob: walk error: %w", err)
		}
	} else {
		// No **, use stdlib filepath.Glob (faster for simple patterns).
		fullPattern := filepath.Join(absBase, args.Pattern)
		paths, err := filepath.Glob(fullPattern)
		if err != nil {
			return "", fmt.Errorf("glob: invalid pattern %q: %w", args.Pattern, err)
		}
		for _, p := range paths {
			if len(matches) >= maxGlobResults {
				break
			}
			info, err := os.Stat(p)
			if err != nil {
				continue
			}
			matches = append(matches, fileEntry{path: p, modTime: info.ModTime()})
		}
	}

	// Sort by modification time, most recent first.
	sort.Slice(matches, func(i, j int) bool {
		return matches[i].modTime.After(matches[j].modTime)
	})

	paths := make([]string, len(matches))
	for i, m := range matches {
		paths[i] = m.path
	}
	return strings.Join(paths, "\n"), nil
}

// matchDoubleStarGlob matches a relative filesystem path against a glob
// pattern that may contain ** (matches zero or more path components).
//
// Both arguments use the OS path separator.
func matchDoubleStarGlob(pattern, path string) bool {
	// Normalise to forward slashes.
	pattern = filepath.ToSlash(pattern)
	path = filepath.ToSlash(path)
	return globMatch(strings.Split(pattern, "/"), strings.Split(path, "/"))
}

// globMatch is the recursive core of matchDoubleStarGlob.
func globMatch(patParts, nameParts []string) bool {
	for len(patParts) > 0 {
		pat := patParts[0]
		patParts = patParts[1:]

		if pat == "**" {
			// ** at the end of the pattern matches everything.
			if len(patParts) == 0 {
				return true
			}
			// Try matching the remaining pattern against every suffix.
			for i := 0; i <= len(nameParts); i++ {
				if globMatch(patParts, nameParts[i:]) {
					return true
				}
			}
			return false
		}

		if len(nameParts) == 0 {
			return false
		}

		matched, err := filepath.Match(pat, nameParts[0])
		if err != nil || !matched {
			return false
		}
		nameParts = nameParts[1:]
	}
	return len(nameParts) == 0
}

// ──────────────────────────────────────────────────────────────────────────────
// Grep
// ──────────────────────────────────────────────────────────────────────────────

// GrepArgs mirrors the Grep tool's JSON parameter schema.
// Fields with hyphenated JSON keys (e.g. "-i") are valid in Go struct tags.
type GrepArgs struct {
	Pattern    string `json:"pattern"`
	Path       string `json:"path"`
	Glob       string `json:"glob"`
	Type       string `json:"type"`
	OutputMode string `json:"output_mode"` // "content" | "files_with_matches" | "count"

	// Ripgrep flags.
	CaseInsens bool  `json:"-i"`
	ShowLineNo *bool `json:"-n"` // pointer so we can detect absence; defaults true in content mode
	Context    int   `json:"context"`
	After      int   `json:"-A"`
	Before     int   `json:"-B"`
	ContextC   int   `json:"-C"`

	// Post-processing.
	HeadLimit int  `json:"head_limit"` // cap output at N lines/entries
	Offset    int  `json:"offset"`     // skip first N lines/entries
	Multiline bool `json:"multiline"`
}

// handleGrep delegates to the system `rg` binary and post-processes its output.
func handleGrep(ctx context.Context, raw json.RawMessage) (string, error) {
	var args GrepArgs
	if err := json.Unmarshal(raw, &args); err != nil {
		return "", fmt.Errorf("grep: invalid args: %w", err)
	}
	if args.Pattern == "" {
		return "", fmt.Errorf("grep: pattern is required")
	}

	// Fast-fail if the session is already shutting down.
	if ctx.Err() != nil {
		return "", fmt.Errorf("grep: cancelled before start")
	}

	rgArgs := buildRgArgs(args)

	// 10 MB cap on rg output — a broad pattern over a large tree can otherwise
	// produce unbounded output and OOM this long-lived process.
	const maxGrepOutputBytes = 10 * 1024 * 1024

	cmd := exec.CommandContext(ctx, "rg", rgArgs...) //nolint:gosec
	var errBuf strings.Builder
	cmd.Stderr = &errBuf

	stdout, pipeErr := cmd.StdoutPipe()
	if pipeErr != nil {
		return "", fmt.Errorf("grep: stdout pipe: %w", pipeErr)
	}

	if err := cmd.Start(); err != nil {
		return "", fmt.Errorf("grep: start: %w", err)
	}

	// Read at most maxGrepOutputBytes; drain remainder to unblock rg.
	data, _ := io.ReadAll(io.LimitReader(stdout, maxGrepOutputBytes))
	_, _ = io.Copy(io.Discard, stdout)

	runErr := cmd.Wait()

	// rg exit codes: 0 = matches found, 1 = no matches, 2 = error.
	output := string(data)

	if runErr == nil {
		// Success path — return stdout only. Stderr (encoding warnings,
		// permission errors) is intentionally excluded to avoid corrupting
		// the structured output the Python caller parses.
		if args.Offset > 0 || args.HeadLimit > 0 {
			output = applySlice(output, args.Offset, args.HeadLimit)
		}
		return output, nil
	}

	// Error path — include stderr for diagnostics.
	if errBuf.Len() > 0 {
		output += errBuf.String()
	}

	if exitErr, ok := runErr.(*exec.ExitError); ok {
		switch exitErr.ExitCode() {
		case 1:
			// No matches — normal, return empty.
			return "", nil
		case -1:
			// Killed by signal — likely context cancellation.
			// Always return an error so dispatch resolves as a failure.
			if len(output) > 0 {
				return "", fmt.Errorf("grep: cancelled (partial output: %s)", output)
			}
			return "", fmt.Errorf("grep: cancelled")
		}
		// Exit code 2+: ripgrep error.
		return "", fmt.Errorf("grep: ripgrep error: %s", output)
	}

	if ctx.Err() != nil {
		return "", fmt.Errorf("grep: cancelled")
	}
	return "", fmt.Errorf("grep: failed to run rg: %w", runErr)
}

// buildRgArgs constructs the ripgrep argument list from GrepArgs.
func buildRgArgs(args GrepArgs) []string {
	var rg []string

	// Suppress "no such file" warnings to stderr.
	rg = append(rg, "--no-messages", "--color=never")

	switch args.OutputMode {
	case "content":
		// Line numbers are on by default in content mode; caller can opt out.
		showLineNo := args.ShowLineNo == nil || *args.ShowLineNo
		if showLineNo {
			rg = append(rg, "-n")
		} else {
			rg = append(rg, "--no-line-number")
		}
		// Always include the filename so the output is unambiguous.
		rg = append(rg, "--with-filename", "--no-heading")
	case "count":
		rg = append(rg, "-c")
	default: // "files_with_matches" or unset — match the original Grep tool default
		rg = append(rg, "-l")
	}

	if args.CaseInsens {
		rg = append(rg, "-i")
	}

	if args.Multiline {
		rg = append(rg, "-U", "--multiline-dotall")
	}

	// Context lines (-C takes precedence over separate -A/-B).
	ctx := args.Context
	if args.ContextC > 0 {
		ctx = args.ContextC
	}
	if ctx > 0 {
		rg = append(rg, "-C", strconv.Itoa(ctx))
	} else {
		if args.After > 0 {
			rg = append(rg, "-A", strconv.Itoa(args.After))
		}
		if args.Before > 0 {
			rg = append(rg, "-B", strconv.Itoa(args.Before))
		}
	}

	// File glob filter.
	if args.Glob != "" {
		rg = append(rg, "-g", args.Glob)
	}

	// File type filter.
	if args.Type != "" {
		rg = append(rg, "--type", args.Type)
	}

	// Pattern.
	rg = append(rg, "-e", args.Pattern)

	// Search root (default to current directory).
	if args.Path != "" {
		rg = append(rg, args.Path)
	} else {
		rg = append(rg, ".")
	}

	return rg
}

// applySlice skips the first `offset` lines and then returns at most
// `headLimit` lines (0 means no limit) from the given newline-delimited text.
func applySlice(text string, offset, headLimit int) string {
	if text == "" {
		return text
	}

	// Preserve trailing newline behaviour.
	hasTrailing := strings.HasSuffix(text, "\n")
	lines := strings.Split(strings.TrimRight(text, "\n"), "\n")

	if offset > 0 {
		if offset >= len(lines) {
			return ""
		}
		lines = lines[offset:]
	}

	if headLimit > 0 && len(lines) > headLimit {
		lines = lines[:headLimit]
	}

	result := strings.Join(lines, "\n")
	if hasTrailing {
		result += "\n"
	}
	return result
}
