package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

func TestHealthzReturns200(t *testing.T) {
	rec := httptest.NewRecorder()
	healthzHandler(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("healthz: got %d, want 200", rec.Code)
	}
}

// The experiment reads "hostname" from / to prove a replacement task is a new
// container. Whatever else the handler reports, this field is load-bearing.
func TestRootReportsThisHostname(t *testing.T) {
	rec := httptest.NewRecorder()
	rootHandler(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("root: got %d, want 200", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("root: body is not JSON: %v", err)
	}
	host, _ := os.Hostname()
	if body["hostname"] != host {
		t.Fatalf("root: hostname = %v, want %q", body["hostname"], host)
	}
}

// crashHandler is deliberately untested: it calls os.Exit, which would kill
// the test process. A subprocess harness costs more than the three lines it
// would cover; the live experiment exercises it for real.
