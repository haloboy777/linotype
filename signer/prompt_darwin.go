package main

import (
	"context"
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

type nativePrompter struct{}

// Text travels as argv, never spliced into the script, so a file name can't become AppleScript.
const confirmScript = `on run argv
	tell me to activate
	set r to display dialog (item 1 of argv) with title "Sign with your token" buttons {"Cancel", "View document", "Continue"} default button "Continue" cancel button "Cancel" with icon caution
	return button returned of r
end run`

const pinScript = `on run argv
	tell me to activate
	set r to display dialog (item 1 of argv) with title "Sign with your token" default answer "" with hidden answer buttons {"Cancel", "Sign"} default button "Sign" cancel button "Cancel" with icon caution
	return text returned of r
end run`

func osascript(ctx context.Context, script, text string) (string, bool, error) {
	out, err := exec.CommandContext(ctx, "osascript", "-e", script, text).Output()
	if ctx.Err() != nil {
		return "", false, nil // the page went away; killing osascript closed its window
	}
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) && strings.Contains(string(ee.Stderr), "-128") {
			return "", false, nil // the person pressed Cancel
		}
		return "", false, fmt.Errorf("couldn't show the signing window: %w", err)
	}
	return strings.TrimRight(string(out), "\r\n"), true, nil
}

func (nativePrompter) Confirm(ctx context.Context, d Document) (bool, error) {
	for {
		button, ok, err := osascript(ctx, confirmScript, confirmText(d))
		if err != nil || !ok {
			return false, err
		}
		if button == "Continue" {
			return true, nil
		}
		_ = exec.Command("open", d.Path).Run()
	}
}

func (nativePrompter) PIN(ctx context.Context, d Document) (string, bool, error) {
	return osascript(ctx, pinScript, pinText(d))
}
