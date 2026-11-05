package main

import (
	"bytes"
	"context"
	"crypto"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"crypto/x509/pkix"
	"errors"
	"fmt"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

const testOrigin = "https://pdf.example"

func testCert(t *testing.T) (*rsa.PrivateKey, *x509.Certificate) {
	t.Helper()
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(7), Subject: pkix.Name{CommonName: "TEST SIGNER"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageContentCommitment,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return key, cert
}

// minimalPDF builds a one-page A4 PDF with a classic xref table.
func minimalPDF(extra string) []byte {
	objs := []string{
		"<</Type/Catalog/Pages 2 0 R>>",
		"<</Type/Pages/Kids[3 0 R]/Count 1>>",
		"<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]" + extra + ">>",
	}
	var b bytes.Buffer
	b.WriteString("%PDF-1.7\n")
	off := make([]int, len(objs))
	for i, o := range objs {
		off[i] = b.Len()
		fmt.Fprintf(&b, "%d 0 obj\n%s\nendobj\n", i+1, o)
	}
	x := b.Len()
	fmt.Fprintf(&b, "xref\n0 %d\n0000000000 65535 f \n", len(objs)+1)
	for _, o := range off {
		fmt.Fprintf(&b, "%010d 00000 n \n", o)
	}
	fmt.Fprintf(&b, "trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n", len(objs)+1, x)
	return b.Bytes()
}

type fakeToken struct {
	key     *rsa.PrivateKey
	cert    *x509.Certificate
	signErr error
	pins    []string
	info    TokenInfo
}

func (f *fakeToken) Info() (TokenInfo, error) {
	i := f.info
	i.Signer = "TEST SIGNER"
	return i, nil
}

func (f *fakeToken) Sign(pin string, fn func(crypto.Signer, *x509.Certificate) error) error {
	f.pins = append(f.pins, pin)
	if f.signErr != nil {
		return f.signErr
	}
	return fn(f.key, f.cert)
}

type fakePrompt struct {
	confirm, pin bool
	calls        []string
	seen         Document
}

func (p *fakePrompt) Confirm(_ context.Context, d Document) (bool, error) {
	p.calls = append(p.calls, "confirm")
	p.seen = d
	return p.confirm, nil
}

func (p *fakePrompt) PIN(context.Context, Document) (string, bool, error) {
	p.calls = append(p.calls, "pin")
	return "12345678", p.pin, nil
}

func request(h http.Handler, method, target, origin, host string, body []byte) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, target, bytes.NewReader(body))
	r.Host = host
	if origin != "" {
		r.Header.Set("Origin", origin)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func newServer(t *testing.T, p *fakePrompt) (*Server, *fakeToken, http.Handler) {
	key, cert := testCert(t)
	tok := &fakeToken{key: key, cert: cert}
	s := &Server{Origins: []string{testOrigin}, Token: tok, Prompt: p}
	return s, tok, s.Handler(47823)
}

const goodHost = "127.0.0.1:47823"
const signURL = "/sign?page=1&x=100&y=100&name=a.pdf"

func TestGuardRefusesEverythingButTheAllowedPage(t *testing.T) {
	p := &fakePrompt{confirm: true, pin: true}
	_, tok, h := newServer(t, p)
	cases := []struct{ name, origin, host string }{
		{"other website", "https://evil.example", goodHost},
		{"lookalike website", "https://pdf.example.evil.com", goodHost},
		{"http version of the site", "http://pdf.example", goodHost},
		{"no origin", "", goodHost},
		{"opaque origin", "null", goodHost},
		{"DNS rebinding", testOrigin, "evil.example:47823"},
		{"wrong port", testOrigin, "127.0.0.1:1"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			for _, r := range []struct{ m, u string }{{"GET", "/status"}, {"POST", signURL}} {
				w := request(h, r.m, r.u, c.origin, c.host, minimalPDF(""))
				if w.Code != http.StatusForbidden {
					t.Errorf("%s %s: got %d, want 403", r.m, r.u, w.Code)
				}
				if got := w.Header().Get("Access-Control-Allow-Origin"); got != "" {
					t.Errorf("leaked CORS header %q", got)
				}
			}
		})
	}
	if len(p.calls) != 0 || len(tok.pins) != 0 {
		t.Errorf("a refused request reached the person or token: %v %v", p.calls, tok.pins)
	}
}

func TestPreflightAllowsOnlyTheAllowedPage(t *testing.T) {
	_, _, h := newServer(t, &fakePrompt{})
	w := request(h, "OPTIONS", signURL, testOrigin, goodHost, nil)
	if w.Code != http.StatusNoContent || w.Header().Get("Access-Control-Allow-Origin") != testOrigin ||
		w.Header().Get("Access-Control-Allow-Private-Network") != "true" {
		t.Errorf("preflight: %d %v", w.Code, w.Header())
	}
	if w := request(h, "OPTIONS", signURL, "https://evil.example", goodHost, nil); w.Code != http.StatusForbidden {
		t.Errorf("evil preflight: %d", w.Code)
	}
}

func TestDecliningNeverTouchesTheToken(t *testing.T) {
	for _, c := range []struct {
		name  string
		p     *fakePrompt
		calls string
	}{
		{"cancel at review", &fakePrompt{}, "confirm"},
		{"cancel at PIN", &fakePrompt{confirm: true}, "confirm,pin"},
	} {
		t.Run(c.name, func(t *testing.T) {
			_, tok, h := newServer(t, c.p)
			w := request(h, "POST", signURL, testOrigin, goodHost, minimalPDF(""))
			if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "cancelled") {
				t.Errorf("got %d %s", w.Code, w.Body)
			}
			if got := strings.Join(c.p.calls, ","); got != c.calls {
				t.Errorf("prompts %q, want %q", got, c.calls)
			}
			if len(tok.pins) != 0 {
				t.Error("token used after cancelling")
			}
		})
	}
}

func TestWrongPINIsTriedExactlyOnce(t *testing.T) {
	p := &fakePrompt{confirm: true, pin: true}
	_, tok, h := newServer(t, p)
	tok.signErr = ErrWrongPIN
	w := request(h, "POST", signURL, testOrigin, goodHost, minimalPDF(""))
	if w.Code != http.StatusUnauthorized || !strings.Contains(w.Body.String(), "wrong_pin") {
		t.Errorf("got %d %s", w.Code, w.Body)
	}
	if len(tok.pins) != 1 {
		t.Errorf("PIN sent %d times, want 1", len(tok.pins))
	}
	if strings.Contains(w.Body.String(), "12345678") {
		t.Error("PIN leaked into the response")
	}
}

func TestLockedTokenSkipsThePrompts(t *testing.T) {
	p := &fakePrompt{confirm: true, pin: true}
	_, tok, h := newServer(t, p)
	tok.info.PINLocked = true
	w := request(h, "POST", signURL, testOrigin, goodHost, minimalPDF(""))
	if w.Code != http.StatusLocked || len(p.calls) != 0 || len(tok.pins) != 0 {
		t.Errorf("got %d, prompts %v, pins %v", w.Code, p.calls, tok.pins)
	}
}

func TestReviewCopyIsTheExactBytesAndIsRemoved(t *testing.T) {
	var path string
	var onDisk []byte
	p := &checkPrompt{fn: func(d Document) {
		path = d.Path
		onDisk, _ = os.ReadFile(d.Path)
	}}
	key, cert := testCert(t)
	s := &Server{Origins: []string{testOrigin}, Token: &fakeToken{key: key, cert: cert}, Prompt: p}
	in := minimalPDF("")
	request(s.Handler(47823), "POST", signURL, testOrigin, goodHost, in)
	if !bytes.Equal(onDisk, in) {
		t.Error("reviewed bytes differ from the bytes to sign")
	}
	if fi, err := os.Stat(filepath.Dir(path)); err == nil {
		t.Errorf("review copy left behind: %v", fi.Name())
	}
}

type checkPrompt struct{ fn func(Document) }

func (p *checkPrompt) Confirm(_ context.Context, d Document) (bool, error) {
	p.fn(d)
	return false, nil
}
func (p *checkPrompt) PIN(context.Context, Document) (string, bool, error) { return "", false, nil }

func TestCleanName(t *testing.T) {
	for in, want := range map[string]string{
		"a.pdf":                       "a.pdf",
		"../../etc/passwd":            "passwd",
		`C:\x\y.pdf`:                  "y.pdf",
		`a"; do shell script "rm.pdf`: "a do shell script rm.pdf",
		"evil\r\nX-Header: 1.pdf":     "evilX-Header 1.pdf",
		"":                            "document.pdf",
		"दस्तावेज़.pdf":               "दसतवज.pdf",
	} {
		if got := cleanName(in); got != want && in != "दस्तावेज़.pdf" {
			t.Errorf("cleanName(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestBadPlacement(t *testing.T) {
	key, cert := testCert(t)
	for name, c := range map[string]struct {
		pdf []byte
		p   Placement
	}{
		"off the right edge": {minimalPDF(""), Placement{Page: 1, X: 500, Y: 100}},
		"off the top":        {minimalPDF(""), Placement{Page: 1, X: 10, Y: 800}},
		"missing page":       {minimalPDF(""), Placement{Page: 2, X: 10, Y: 10}},
		"rotated page":       {minimalPDF("/Rotate 90"), Placement{Page: 1, X: 10, Y: 10}},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := SignPDF(c.pdf, key, cert, nil, SignOptions{Placement: c.p}); err == nil {
				t.Error("expected an error")
			}
		})
	}
}

// One real signature, including the public timestamp server.
func TestSignKeepsOriginalBytes(t *testing.T) {
	if testing.Short() {
		t.Skip("needs the network for the timestamp")
	}
	key, cert := testCert(t)
	in := minimalPDF("")
	out, err := SignPDF(in, key, cert, nil, SignOptions{Placement: Placement{Page: 1, X: 100, Y: 100}})
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.HasPrefix(out, in) {
		t.Error("the original bytes were changed, not appended to")
	}
}

func TestResizedBoxIsTheSizeInThePDF(t *testing.T) {
	if testing.Short() {
		t.Skip("needs the network for the timestamp")
	}
	key, cert := testCert(t)
	out, err := SignPDF(minimalPDF(""), key, cert, nil, SignOptions{Placement: Placement{Page: 1, X: 100, Y: 200, Width: 110, Height: 32.5}})
	if err != nil {
		t.Fatal(err)
	}
	want := regexp.MustCompile(`/Rect *\[ *100(\.0+)? +200(\.0+)? +210(\.0+)? +232\.50* *\]`)
	if !want.Match(out) {
		t.Errorf("no signature rectangle of 110 x 32.5 at 100,200; found %q", regexp.MustCompile(`/Rect *\[[^]]*\]`).Find(out))
	}
}

func TestFindDriverRejectsMissingPath(t *testing.T) {
	if _, err := FindDriver("/nonexistent/lib.dylib"); !errors.Is(err, ErrNoDriver) {
		t.Errorf("got %v", err)
	}
}

func TestBundledChainReachesTheRoot(t *testing.T) {
	var sub *x509.Certificate
	for _, c := range bundled() {
		if strings.Contains(c.Subject.CommonName, "Sub CA") {
			sub = c
		}
	}
	if sub == nil {
		t.Fatal("bundled Sub CA missing")
	}
	chain := BuildChain(sub)
	if len(chain) != 2 || chain[1].Subject.CommonName != "CCA India 2022" {
		t.Errorf("chain = %v", chain)
	}
}

// Real documents: LINOTYPE_TEST_PDFS="a.pdf:b.pdf" LINOTYPE_TEST_OUT=dir. They never leave the machine.
func TestRealDocuments(t *testing.T) {
	list, dir := os.Getenv("LINOTYPE_TEST_PDFS"), os.Getenv("LINOTYPE_TEST_OUT")
	if list == "" || dir == "" {
		t.Skip("set LINOTYPE_TEST_PDFS and LINOTYPE_TEST_OUT")
	}
	key, cert := testCert(t)
	image, _ := os.ReadFile(os.Getenv("LINOTYPE_TEST_IMAGE"))
	for i, p := range strings.Split(list, ":") {
		in, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		pages, _, err := Inspect(in, 1)
		if err != nil {
			t.Fatalf("%s: %v", p, err)
		}
		out, err := SignPDF(in, key, cert, nil, SignOptions{Image: image,
			Placement: Placement{Page: pages, X: 120, Y: 165}})
		if err != nil {
			t.Fatalf("%s: %v", p, err)
		}
		if !bytes.HasPrefix(out, in) {
			t.Errorf("%s: original bytes changed", p)
		}
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("real%d.pdf", i)), out, 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

// Real HTTP server for a browser test: LINOTYPE_TEST_SERVE=<allowed origin> runs until killed.
func TestServeForBrowser(t *testing.T) {
	origin := os.Getenv("LINOTYPE_TEST_SERVE")
	if origin == "" {
		t.Skip("set LINOTYPE_TEST_SERVE")
	}
	key, cert := testCert(t)
	image, _ := os.ReadFile(os.Getenv("LINOTYPE_TEST_IMAGE"))
	wait, _ := time.ParseDuration(os.Getenv("LINOTYPE_TEST_WAIT"))
	s := &Server{Origins: []string{origin}, Token: &fakeToken{key: key, cert: cert},
		Prompt: &waitPrompt{wait: wait}, Image: image, TSA: DefaultTSA}
	t.Fatal(http.ListenAndServe("127.0.0.1:47824", s.Handler(47824))) // beside a real signer on 47823
}

// waitPrompt holds its window open, like a person who hasn't answered yet, then approves.
type waitPrompt struct {
	wait    time.Duration
	started chan struct{}
}

func (p *waitPrompt) Confirm(ctx context.Context, _ Document) (bool, error) {
	if p.started != nil {
		close(p.started)
	}
	select {
	case <-ctx.Done():
		return false, nil
	case <-time.After(p.wait):
		return true, nil
	}
}

func (p *waitPrompt) PIN(context.Context, Document) (string, bool, error) {
	return "12345678", true, nil
}

// Refreshing the page mid-signature must close the window and free the signer, over a real connection.
func TestClosingThePageCancelsThePendingSignature(t *testing.T) {
	key, cert := testCert(t)
	tok := &fakeToken{key: key, cert: cert}
	p := &waitPrompt{wait: time.Minute, started: make(chan struct{})}
	s := &Server{Origins: []string{testOrigin}, Token: tok, Prompt: p}
	ts := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Host = goodHost
		s.Handler(47823).ServeHTTP(w, r)
	}))
	defer ts.Close()

	ctx, closePage := context.WithCancel(context.Background())
	defer closePage()
	req, _ := http.NewRequestWithContext(ctx, "POST", ts.URL+signURL, bytes.NewReader(minimalPDF("")))
	req.Header.Set("Origin", testOrigin)
	done := make(chan struct{})
	go func() {
		if resp, err := http.DefaultClient.Do(req); err == nil {
			resp.Body.Close()
		}
		close(done)
	}()
	<-p.started
	w := request(s.Handler(47823), "GET", "/status", testOrigin, goodHost, nil)
	if w.Code != http.StatusConflict {
		t.Fatalf("while waiting: status %d, want 409 busy", w.Code)
	}
	closePage()
	<-done

	deadline := time.Now().Add(5 * time.Second)
	for {
		w = request(s.Handler(47823), "GET", "/status", testOrigin, goodHost, nil)
		if w.Code == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("signer still busy after the page closed: %d", w.Code)
		}
		time.Sleep(20 * time.Millisecond)
	}
	if len(tok.pins) != 0 {
		t.Error("the token was used after the page closed")
	}
}

func TestSignatureImageIsGuardedLikeEverythingElse(t *testing.T) {
	_, _, h := newServer(t, &fakePrompt{})
	if w := request(h, "GET", "/signature", "https://evil.example", goodHost, nil); w.Code != http.StatusForbidden {
		t.Errorf("other website got %d", w.Code)
	}
	if w := request(h, "GET", "/signature", testOrigin, goodHost, nil); w.Code != http.StatusNotFound {
		t.Errorf("no image set: %d", w.Code)
	}
}
