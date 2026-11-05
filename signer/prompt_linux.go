package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

type nativePrompter struct{}

const zenity = "zenity"

func zenityRun(ctx context.Context, args ...string) (string, int, error) {
	out, err := exec.CommandContext(ctx, zenity, args...).Output()
	if ctx.Err() != nil {
		return "", 1, nil // the page went away; killing zenity closed its window
	}
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return strings.TrimRight(string(out), "\n"), ee.ExitCode(), nil
		}
		return "", -1, fmt.Errorf("couldn't show the signing window (is zenity installed?): %w", err)
	}
	return strings.TrimRight(string(out), "\n"), 0, nil
}

func (nativePrompter) Confirm(ctx context.Context, d Document) (bool, error) {
	for {
		out, code, err := zenityRun(ctx, "--question", "--no-markup", "--title=Sign with your token", "--text="+confirmText(d),
			"--ok-label=Continue", "--cancel-label=Cancel", "--extra-button=View document")
		if err != nil {
			return false, err
		}
		if code == 0 {
			return true, nil
		}
		if out != "View document" {
			return false, nil
		}
		_ = exec.Command("xdg-open", d.Path).Start()
	}
}

func (nativePrompter) PIN(ctx context.Context, d Document) (string, bool, error) {
	out, code, err := zenityRun(ctx, "--entry", "--hide-text", "--no-markup", "--title=Sign with your token", "--text="+pinText(d),
		"--ok-label=Sign", "--cancel-label=Cancel")
	if err != nil {
		return "", false, err
	}
	return out, code == 0, nil
}
