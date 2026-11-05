package main

import (
	"crypto/x509"
	"embed"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"time"
)

//go:embed certs/*.pem
var certFiles embed.FS

// Public CA certificates shipped with the binary. CCA's own download URLs are often unreachable.
func bundled() []*x509.Certificate {
	var out []*x509.Certificate
	entries, _ := certFiles.ReadDir("certs")
	for _, e := range entries {
		b, _ := certFiles.ReadFile("certs/" + e.Name())
		if block, _ := pem.Decode(b); block != nil {
			if c, err := x509.ParseCertificate(block.Bytes); err == nil {
				out = append(out, c)
			}
		}
	}
	return out
}

// BuildChain walks from the signer's certificate towards the root. It uses the bundled CAs first,
// then the certificate's own "CA Issuers" link. A signer from another CA therefore works too.
func BuildChain(leaf *x509.Certificate) []*x509.Certificate {
	pool := bundled()
	client := &http.Client{Timeout: 5 * time.Second}
	var chain []*x509.Certificate
	cur := leaf
	for len(chain) < 6 && !isRoot(cur) {
		next := findIssuer(cur, pool)
		if next == nil {
			next = fetchIssuer(client, cur)
		}
		if next == nil {
			break
		}
		chain = append(chain, next)
		cur = next
	}
	return chain
}

func isRoot(c *x509.Certificate) bool {
	return c.Subject.String() == c.Issuer.String() && c.CheckSignatureFrom(c) == nil
}

func findIssuer(c *x509.Certificate, pool []*x509.Certificate) *x509.Certificate {
	for _, p := range pool {
		if c.CheckSignatureFrom(p) == nil {
			return p
		}
	}
	return nil
}

func fetchIssuer(client *http.Client, c *x509.Certificate) *x509.Certificate {
	for _, u := range c.IssuingCertificateURL {
		if got, err := fetchCert(client, u); err == nil && c.CheckSignatureFrom(got) == nil {
			return got
		}
	}
	return nil
}

func fetchCert(client *http.Client, url string) (*x509.Certificate, error) {
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: %s", url, resp.Status)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if block, _ := pem.Decode(b); block != nil {
		b = block.Bytes
	}
	return x509.ParseCertificate(b)
}
