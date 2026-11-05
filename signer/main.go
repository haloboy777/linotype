// linotype-signer lets pdf.ayushsingh.dev sign PDFs with a USB token. It runs on your own computer:
// the website hands it a PDF, you approve in a native window, and the token signs. The PIN and the
// private key never reach the website.
package main

import (
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	Version       = "0.1.0"
	DefaultPort   = 47823
	DefaultOrigin = "https://pdf.ayushsingh.dev"
)

func imagePath() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		return ""
	}
	return filepath.Join(dir, "linotype-signer", "signature.png")
}

func loadImage() []byte {
	b, err := os.ReadFile(imagePath())
	if err != nil {
		return nil
	}
	return b
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	cmd := "serve"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		cmd, args = args[0], args[1:]
	}
	switch cmd {
	case "serve":
		return serve(args)
	case "doctor":
		return doctor(args)
	case "version":
		fmt.Println(Version)
		return nil
	}
	return fmt.Errorf("unknown command %q (use: serve, doctor, version)", cmd)
}

func serve(args []string) error {
	fs := flag.NewFlagSet("serve", flag.ContinueOnError)
	port := fs.Int("port", DefaultPort, "port on 127.0.0.1")
	lib := fs.String("lib", "", "PKCS#11 driver path (default: search known locations)")
	origins := fs.String("origin", DefaultOrigin, "comma-separated websites allowed to request signatures")
	tsa := fs.String("tsa", DefaultTSA, "timestamp server")
	if err := fs.Parse(args); err != nil {
		return err
	}
	path, err := FindDriver(*lib)
	if err != nil {
		return err
	}
	s := &Server{
		Origins: strings.Split(*origins, ","), Token: P11Token{Lib: path},
		Prompt: nativePrompter{}, Image: loadImage(), TSA: *tsa,
	}
	addr := net.JoinHostPort("127.0.0.1", strconv.Itoa(*port))
	log.Printf("linotype-signer %s on http://%s, allowing %s, driver %s, signature image: %t",
		Version, addr, *origins, path, s.Image != nil)
	srv := &http.Server{Addr: addr, Handler: s.Handler(*port), ReadHeaderTimeout: 10 * time.Second}
	return srv.ListenAndServe()
}

// doctor reads the token without a PIN and reports what it finds.
func doctor(args []string) error {
	fs := flag.NewFlagSet("doctor", flag.ContinueOnError)
	lib := fs.String("lib", "", "PKCS#11 driver path")
	if err := fs.Parse(args); err != nil {
		return err
	}
	path, err := FindDriver(*lib)
	if err != nil {
		return err
	}
	fmt.Println("driver:   ", path)
	img := "none (text-only stamp), expected at " + imagePath()
	if loadImage() != nil {
		img = imagePath()
	}
	fmt.Println("signature:", img)
	info, err := (P11Token{Lib: path}).Info()
	if err != nil {
		if errors.Is(err, ErrNoToken) {
			return err
		}
		return fmt.Errorf("reading the token: %w", err)
	}
	fmt.Printf("token:     %s (%s)\nsigner:    %s, valid until %s\npin:       low=%t finalTry=%t locked=%t\n",
		info.Label, info.Serial, info.Signer, info.NotAfter.Format("2 Jan 2006"), info.PINCountLow, info.PINFinalTry, info.PINLocked)
	return nil
}
