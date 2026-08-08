// Canary for the floci ECS-reconciliation experiment: a server whose only job
// is to identify which instance of itself is answering, and to die on demand.
// See docs/superpowers/specs/2026-08-08-floci-evaluation-design.md.
package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

var startTime = time.Now()

func healthzHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "ok")
}

// crashHandler kills the process from the inside. Killing from outside
// (docker kill) would conflate floci noticing with Docker's restart policy
// noticing; a non-zero exit from within isolates ECS reconciliation as the
// only thing that could bring a replacement back.
func crashHandler(w http.ResponseWriter, r *http.Request) {
	log.Println("crash requested, exiting 3")
	os.Exit(3)
}

// rootHandler reports which instance is answering — the instrument the whole
// experiment reads. A replacement container has a new hostname (Docker sets it
// to the container ID); uptime ~0 and a fresh pid corroborate a new process
// rather than a reconnect. The aws_env field shows whatever ECS-style metadata
// floci injects into tasks — real ECS sets AWS_EXECUTION_ENV and friends, and
// whether floci bothers is itself a finding.
func rootHandler(w http.ResponseWriter, r *http.Request) {
	host, _ := os.Hostname()
	awsEnv := []string{}
	for _, kv := range os.Environ() {
		if strings.HasPrefix(kv, "AWS_") || strings.HasPrefix(kv, "ECS_") {
			awsEnv = append(awsEnv, kv)
		}
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]any{
		"hostname": host,
		"pid":      os.Getpid(),
		"started":  startTime.Format(time.RFC3339),
		"uptime":   time.Since(startTime).Round(time.Second).String(),
		"aws_env":  awsEnv,
	})
}

func main() {
	http.HandleFunc("/", rootHandler)
	http.HandleFunc("/healthz", healthzHandler)
	http.HandleFunc("/crash", crashHandler)
	log.Println("canary listening on :8080")
	log.Fatal(http.ListenAndServe(":8080", nil))
}
