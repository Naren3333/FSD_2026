package platform

import (
	"context"
	"fmt"
	"net/http"
	"os"
	"time"
)

// healthcheckflag is the argument that makes a service binary check its own readiness instead of serving.
const HealthcheckFlag = "-healthcheck"

// healthchecktimeout bounds the whole check, below the compose healthcheck's own timeout.
const healthcheckTimeout = 3 * time.Second

// healthcheck returns at once unless the process was started with healthcheckflag.
func Healthcheck() {
	if len(os.Args) < 2 || os.Args[1] != HealthcheckFlag {
		return
	}
	if err := checkReady("http://127.0.0.1:" + Env("PORT", "8080") + "/health/ready"); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	os.Exit(0)
}

// checkready returns nil when url answers 200 within healthchecktimeout.
func checkReady(url string) error {
	ctx, cancel := context.WithTimeout(context.Background(), healthcheckTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, http.NoBody)
	if err != nil {
		return err
	}
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("not ready: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return fmt.Errorf("not ready: %s answered %d", url, res.StatusCode)
	}
	return nil
}
