// Package main implements the AHS Bwrapped Command Handler (BCH) proxy.
//
// The proxy reads newline-delimited JSON requests from stdin, dispatches each
// to the appropriate local tool handler in a goroutine, and writes newline-
// delimited JSON responses to stdout.  req_id ties each response to its
// request, allowing the Python caller to use asyncio.Future objects for
// fully concurrent dispatch.
//
// Wire protocol:
//
//	Request  (stdin, one per line):
//	  {"req_id":"<uuid>","tool":"<name>","args":{...}}
//
//	Response (stdout, one per line):
//	  {"req_id":"<uuid>","ok":true,"result":"..."}
//	  {"req_id":"<uuid>","ok":false,"error":"..."}
//
// On SIGTERM or stdin EOF the proxy cancels all in-flight requests (they
// resolve with an error response) and exits cleanly.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/signal"
	"sync"
	"syscall"
)

// ──────────────────────────────────────────────────────────────────────────────
// Wire protocol types
// ──────────────────────────────────────────────────────────────────────────────

// Request is a single tool invocation sent by the Python caller.
type Request struct {
	ReqID string          `json:"req_id"`
	Tool  string          `json:"tool"`
	Args  json.RawMessage `json:"args"`
}

// Response is the result of a single tool invocation.
type Response struct {
	ReqID  string `json:"req_id"`
	OK     bool   `json:"ok"`
	Result string `json:"result,omitempty"`
	Error  string `json:"error,omitempty"`
}

// ──────────────────────────────────────────────────────────────────────────────
// Thread-safe stdout writer
// ──────────────────────────────────────────────────────────────────────────────

type safeWriter struct {
	mu sync.Mutex
	bw *bufio.Writer
}

func newSafeWriter(w io.Writer) *safeWriter {
	return &safeWriter{bw: bufio.NewWriter(w)}
}

func (sw *safeWriter) write(resp Response) {
	data, err := json.Marshal(resp)
	if err != nil {
		// Should never happen with our types, but guard anyway.
		return
	}
	sw.mu.Lock()
	defer sw.mu.Unlock()
	_, _ = sw.bw.Write(data)
	_ = sw.bw.WriteByte('\n')
	if err := sw.bw.Flush(); err != nil {
		log.Printf("bch: failed to flush response for req_id %s: %v", resp.ReqID, err)
	}
}

func (sw *safeWriter) ok(reqID, result string) {
	sw.write(Response{ReqID: reqID, OK: true, Result: result})
}

func (sw *safeWriter) fail(reqID, errMsg string) {
	sw.write(Response{ReqID: reqID, OK: false, Error: errMsg})
}

// ──────────────────────────────────────────────────────────────────────────────
// Dispatcher
// ──────────────────────────────────────────────────────────────────────────────

func dispatch(ctx context.Context, out *safeWriter, req Request) {
	var (
		result string
		err    error
	)

	switch req.Tool {
	case "Bash":
		result, err = handleBash(ctx, req.Args)
	case "Read":
		result, err = handleRead(ctx, req.Args)
	case "Write":
		result, err = handleWrite(ctx, req.Args)
	case "Edit":
		result, err = handleEdit(ctx, req.Args)
	case "Glob":
		result, err = handleGlob(ctx, req.Args)
	case "Grep":
		result, err = handleGrep(ctx, req.Args)
	default:
		err = fmt.Errorf("unknown tool %q", req.Tool)
	}

	if err != nil {
		out.fail(req.ReqID, err.Error())
	} else {
		out.ok(req.ReqID, result)
	}
}

// ──────────────────────────────────────────────────────────────────────────────
// Bounded line reader
// ──────────────────────────────────────────────────────────────────────────────

// readBoundedLine reads one newline-terminated line from r, returning at most
// maxBytes bytes of content (newline excluded).  If the line is longer than
// maxBytes the remainder is silently discarded and ok=false is returned, so the
// caller can skip the oversized request rather than letting a single bad line
// kill the whole proxy (cf. bufio.Scanner's "token too long" fatal behaviour).
func readBoundedLine(r *bufio.Reader, maxBytes int) (line []byte, ok bool, err error) {
	var buf []byte
	oversized := false
	for {
		frag, isPrefix, rerr := r.ReadLine()
		if !oversized {
			if len(buf)+len(frag) <= maxBytes {
				buf = append(buf, frag...)
			} else {
				oversized = true // discard from here on
			}
		}
		// isPrefix==false means we've consumed the full line (including newline).
		if !isPrefix {
			return buf, !oversized, rerr
		}
		if rerr != nil {
			return nil, false, rerr
		}
	}
}

// ──────────────────────────────────────────────────────────────────────────────
// Main
// ──────────────────────────────────────────────────────────────────────────────

func main() {
	log.SetOutput(os.Stderr)
	log.SetFlags(0)

	out := newSafeWriter(os.Stdout)

	// Root context — cancelled on SIGTERM/SIGINT or stdin EOF.
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	// Handle termination signals: cancel context so all in-flight goroutines
	// get a ctx.Err() and write an error response before exiting.
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
	go func() {
		select {
		case sig := <-sigCh:
			log.Printf("bch: received signal %v — draining pending requests", sig)
			cancel()
			// Close stdin so the reader loop returns EOF immediately, allowing
			// the main goroutine to break out and drain in-flight tasks.
			_ = os.Stdin.Close()
		case <-ctx.Done():
		}
	}()

	// WaitGroup ensures we don't exit before all goroutines have written
	// their (possibly error) responses.
	var wg sync.WaitGroup

	// Use a bounded reader instead of bufio.Scanner so that a single oversized
	// request line fails only that request (not the whole process).
	reader := bufio.NewReaderSize(os.Stdin, 4096)
	const maxRequestLineBytes = 10 * 1024 * 1024 // 10 MiB

	for {
		if ctx.Err() != nil {
			break
		}

		line, ok, err := readBoundedLine(reader, maxRequestLineBytes)
		if !ok {
			// Oversized line — extract req_id from raw bytes without requiring
			// valid JSON (partial lines are syntactically incomplete and will
			// always fail json.Unmarshal).  req_id is always near the start of
			// the request so the partial bytes reliably contain it.
			// Handles both compact ("req_id":"x") and spaced ("req_id": "x") JSON.
			errMsg := fmt.Sprintf("request line exceeded %d MiB", maxRequestLineBytes/(1024*1024))
			log.Printf("bch: %s — dropping", errMsg)
			if len(line) > 0 {
				if idx := bytes.Index(line, []byte(`"req_id"`)); idx >= 0 {
					// Skip past the key name, then whitespace/colon/whitespace to reach the value.
					rest := bytes.TrimLeft(line[idx+len(`"req_id"`):], " \t\r\n:")
					if len(rest) > 0 && rest[0] == '"' {
						rest = rest[1:] // skip opening quote
						if end := bytes.IndexByte(rest, '"'); end >= 0 {
							out.fail(string(rest[:end]), errMsg)
						}
					}
				}
			}
		} else {
			line = bytes.TrimSpace(line)
			if len(line) > 0 {
				var req Request
				if jsonErr := json.Unmarshal(line, &req); jsonErr != nil {
					log.Printf("bch: failed to parse request: %v (line: %s)", jsonErr, line)
				} else if req.ReqID == "" {
					log.Printf("bch: request missing req_id, ignoring")
				} else {
					wg.Add(1)
					go func(r Request) {
						defer wg.Done()
						dispatch(ctx, out, r)
					}(req)
				}
			}
		}

		if err != nil {
			if err != io.EOF {
				log.Printf("bch: stdin read error: %v", err)
			}
			break
		}
	}

	// stdin closed (EOF) — cancel context so in-flight goroutines know to
	// wrap up, then wait for them all to finish.
	cancel()
	wg.Wait()
}
