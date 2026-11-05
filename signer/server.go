package main

import (
	"context"
	"crypto"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"unicode"
)

const maxPDF = 60 << 20

// Document is what the person sees before agreeing to sign.
type Document struct {
	Name   string
	Pages  int
	SHA256 string
	Page   int
	Signer string
	Warn   string
	Path   string // temporary copy, for the View button
}

// Prompter asks the person directly, in a native window a web page can't draw or read.
// Both methods report ok=false when the person cancels or ctx ends, which closes the window:
// refreshing or closing the page must not leave a signature waiting.
type Prompter interface {
	Confirm(ctx context.Context, d Document) (ok bool, err error)
	PIN(ctx context.Context, d Document) (pin string, ok bool, err error)
}

type Server struct {
	Origins []string
	Token   Token
	Prompt  Prompter
	Image   []byte
	TSA     string
	mu      sync.Mutex // one signature at a time
}

func (s *Server) Handler(port int) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /status", s.status)
	mux.HandleFunc("POST /sign", s.sign)
	mux.HandleFunc("GET /signature", s.signature)
	return s.guard(port, mux)
}

// guard is the only thing between any web page and the token, so it fails closed:
//   - Host must be this loopback address, which stops DNS rebinding.
//   - Origin must be on the allow list. Browsers set it and page scripts can't change it.
//     A request with no Origin isn't from a page we trust, so it's refused too.
func (s *Server) guard(port int, next http.Handler) http.Handler {
	hosts := map[string]bool{
		net.JoinHostPort("127.0.0.1", strconv.Itoa(port)): true,
		net.JoinHostPort("localhost", strconv.Itoa(port)): true,
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !hosts[r.Host] {
			fail(w, http.StatusForbidden, "bad_host", "Requests must be addressed to this computer.")
			return
		}
		origin := r.Header.Get("Origin")
		if !s.allowed(origin) {
			fail(w, http.StatusForbidden, "bad_origin", "This website isn't allowed to use your signing token.")
			return
		}
		h := w.Header()
		h.Set("Access-Control-Allow-Origin", origin)
		h.Set("Vary", "Origin")
		h.Set("Access-Control-Expose-Headers", "X-Signed-Name")
		h.Set("Cache-Control", "no-store")
		if r.Method == http.MethodOptions {
			h.Set("Access-Control-Allow-Methods", "GET, POST")
			h.Set("Access-Control-Allow-Headers", "Content-Type")
			h.Set("Access-Control-Allow-Private-Network", "true")
			h.Set("Access-Control-Max-Age", "600")
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func (s *Server) allowed(origin string) bool {
	for _, o := range s.Origins {
		if origin != "" && origin == o {
			return true
		}
	}
	return false
}

func fail(w http.ResponseWriter, status int, code, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"code": code, "error": msg})
}

// failErr turns a token error into a response. Internal details go nowhere near a PIN.
func failErr(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, ErrNoDriver):
		fail(w, http.StatusFailedDependency, "no_driver", err.Error())
	case errors.Is(err, ErrNoToken), errors.Is(err, ErrNoCert):
		fail(w, http.StatusFailedDependency, "no_token", err.Error())
	case errors.Is(err, ErrWrongPIN):
		fail(w, http.StatusUnauthorized, "wrong_pin", err.Error())
	case errors.Is(err, ErrPINLocked):
		fail(w, http.StatusLocked, "pin_locked", err.Error())
	default:
		fail(w, http.StatusInternalServerError, "failed", "Signing failed: "+err.Error())
	}
}

func (s *Server) status(w http.ResponseWriter, _ *http.Request) {
	if !s.mu.TryLock() {
		fail(w, http.StatusConflict, "busy", "Another signature is waiting for you.")
		return
	}
	defer s.mu.Unlock()
	info, err := s.Token.Info()
	if err != nil {
		failErr(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"ok": true, "version": Version, "signer": info.Signer, "expires": info.NotAfter, "hasImage": len(s.Image) > 0,
		"pinFinalTry": info.PINFinalTry, "pinCountLow": info.PINCountLow, "pinLocked": info.PINLocked,
	})
}

// signature serves the handwritten image so the page can preview the real stamp.
func (s *Server) signature(w http.ResponseWriter, _ *http.Request) {
	if len(s.Image) == 0 {
		fail(w, http.StatusNotFound, "no_image", "No signature image is set up.")
		return
	}
	w.Header().Set("Content-Type", http.DetectContentType(s.Image))
	_, _ = w.Write(s.Image)
}

func (s *Server) sign(w http.ResponseWriter, r *http.Request) {
	if !s.mu.TryLock() {
		fail(w, http.StatusConflict, "busy", "Another signature is waiting for you.")
		return
	}
	defer s.mu.Unlock()

	q := r.URL.Query()
	place, err := parsePlacement(q)
	if err != nil {
		fail(w, http.StatusBadRequest, "bad_request", err.Error())
		return
	}
	pdf, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxPDF))
	if err != nil {
		fail(w, http.StatusRequestEntityTooLarge, "too_large", "This PDF is too large to sign.")
		return
	}
	pages, _, err := Inspect(pdf, place.Page)
	if err != nil {
		fail(w, http.StatusBadRequest, "bad_pdf", err.Error())
		return
	}
	info, err := s.Token.Info()
	if err != nil {
		failErr(w, err)
		return
	}
	if info.PINLocked {
		failErr(w, ErrPINLocked)
		return
	}

	// The person reviews the bytes that will be signed, from a copy the website can't change.
	copyPath, removeCopy, err := tempCopy(pdf)
	if err != nil {
		failErr(w, err)
		return
	}
	defer removeCopy()
	sum := sha256.Sum256(pdf)
	doc := Document{
		Name: cleanName(q.Get("name")), Pages: pages, Page: place.Page, Signer: info.Signer,
		SHA256: hex.EncodeToString(sum[:]), Path: copyPath,
	}
	switch {
	case info.PINFinalTry:
		doc.Warn = "⚠️ LAST PIN ATTEMPT. A wrong PIN will lock this token."
	case info.PINCountLow:
		doc.Warn = "⚠️ Wrong PINs were entered before. Few attempts remain."
	}

	ctx := r.Context() // ends when the page refreshes or closes
	ok, err := s.Prompt.Confirm(ctx, doc)
	if err != nil {
		failErr(w, err)
		return
	}
	if !ok {
		fail(w, http.StatusConflict, "cancelled", "Signing cancelled.")
		return
	}
	pin, ok, err := s.Prompt.PIN(ctx, doc)
	if err != nil {
		failErr(w, err)
		return
	}
	if !ok || pin == "" || ctx.Err() != nil {
		fail(w, http.StatusConflict, "cancelled", "Signing cancelled.")
		return
	}

	var out []byte
	err = s.Token.Sign(pin, func(signer crypto.Signer, cert *x509.Certificate) error {
		var e error
		out, e = SignPDF(pdf, signer, cert, BuildChain(cert), SignOptions{Placement: place, Image: s.Image, TSA: s.TSA})
		return e
	})
	if err != nil {
		failErr(w, err)
		return
	}
	h := w.Header()
	h.Set("Content-Type", "application/pdf")
	h.Set("Content-Length", strconv.Itoa(len(out)))
	h.Set("X-Signed-Name", url.PathEscape(signedName(doc.Name)))
	_, _ = w.Write(out)
}

func parsePlacement(q url.Values) (Placement, error) {
	page, err := strconv.Atoi(q.Get("page"))
	if err != nil || page < 1 {
		return Placement{}, errors.New("page must be a page number")
	}
	num := func(k string, def float64) (float64, error) {
		if q.Get(k) == "" && def >= 0 {
			return def, nil
		}
		v, err := strconv.ParseFloat(q.Get(k), 64)
		if err != nil || v < 0 || v > 20000 {
			return 0, fmt.Errorf("%s must be a position in points", k)
		}
		return v, nil
	}
	p := Placement{Page: page}
	if p.X, err = num("x", -1); err != nil {
		return p, err
	}
	if p.Y, err = num("y", -1); err != nil {
		return p, err
	}
	if p.Width, err = num("w", DefaultBoxWidth); err != nil {
		return p, err
	}
	if p.Height, err = num("h", DefaultBoxHeight); err != nil {
		return p, err
	}
	return p, nil
}

// cleanName keeps a website-supplied name harmless: it's shown in a native window and used in a header.
func cleanName(name string) string {
	name = filepath.Base(strings.ReplaceAll(name, `\`, "/"))
	var b strings.Builder
	for _, r := range name {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || strings.ContainsRune(" ._-()", r) {
			b.WriteRune(r)
		}
	}
	out := strings.TrimSpace(b.String())
	if runes := []rune(out); len(runes) > 80 {
		out = string(runes[len(runes)-80:])
	}
	if out == "" || out == "." {
		return "document.pdf"
	}
	return out
}

func signedName(name string) string {
	return strings.TrimSuffix(name, filepath.Ext(name)) + "-signed.pdf"
}

func tempCopy(pdf []byte) (string, func(), error) {
	dir, err := os.MkdirTemp("", "linotype-signer-")
	if err != nil {
		return "", nil, err
	}
	path := filepath.Join(dir, "review.pdf")
	if err := os.WriteFile(path, pdf, 0o600); err != nil {
		_ = os.RemoveAll(dir)
		return "", nil, err
	}
	return path, func() { _ = os.RemoveAll(dir) }, nil
}
