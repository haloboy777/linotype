package main

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type nativePrompter struct{}

// Text travels in an environment variable, never spliced into the script.
const psConfirm = `Add-Type -AssemblyName System.Windows.Forms
$r=[System.Windows.Forms.MessageBox]::Show($env:LINOTYPE_TEXT+"` + "`n`n" + `Yes = continue, No = view the document first","Sign with your token","YesNoCancel","Warning")
Write-Output $r`

const psPIN = `Add-Type -AssemblyName System.Windows.Forms
$f=New-Object Windows.Forms.Form;$f.Text='Sign with your token';$f.Width=460;$f.Height=240;$f.StartPosition='CenterScreen';$f.TopMost=$true
$l=New-Object Windows.Forms.Label;$l.Text=$env:LINOTYPE_TEXT;$l.Left=12;$l.Top=12;$l.Width=420;$l.Height=100;$f.Controls.Add($l)
$t=New-Object Windows.Forms.TextBox;$t.UseSystemPasswordChar=$true;$t.Left=12;$t.Top=120;$t.Width=420;$f.Controls.Add($t)
$o=New-Object Windows.Forms.Button;$o.Text='Sign';$o.Left=270;$o.Top=155;$o.DialogResult='OK';$f.Controls.Add($o);$f.AcceptButton=$o
$c=New-Object Windows.Forms.Button;$c.Text='Cancel';$c.Left=355;$c.Top=155;$c.DialogResult='Cancel';$f.Controls.Add($c);$f.CancelButton=$c
if($f.ShowDialog() -eq 'OK'){[Console]::Out.Write($t.Text)}else{exit 2}`

func powershell(ctx context.Context, script, text string) (string, int, error) {
	cmd := exec.CommandContext(ctx, "powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script)
	cmd.Env = append(os.Environ(), "LINOTYPE_TEXT="+text)
	out, err := cmd.Output()
	if ctx.Err() != nil {
		return "", 2, nil // the page went away; killing PowerShell closed its window
	}
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) {
			return "", ee.ExitCode(), nil
		}
		return "", -1, fmt.Errorf("couldn't show the signing window: %w", err)
	}
	return strings.TrimRight(string(out), "\r\n"), 0, nil
}

func (nativePrompter) Confirm(ctx context.Context, d Document) (bool, error) {
	for {
		out, _, err := powershell(ctx, psConfirm, confirmText(d))
		if err != nil {
			return false, err
		}
		switch out {
		case "Yes":
			return true, nil
		case "No":
			_ = exec.Command("cmd", "/c", "start", "", d.Path).Start()
		default:
			return false, nil
		}
	}
}

func (nativePrompter) PIN(ctx context.Context, d Document) (string, bool, error) {
	out, code, err := powershell(ctx, psPIN, pinText(d))
	if err != nil {
		return "", false, err
	}
	return out, code == 0, nil
}
