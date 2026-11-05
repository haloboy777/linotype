package main

import "fmt"

func confirmText(d Document) string {
	s := fmt.Sprintf("A website wants you to sign this document:\n\n%s\n%d page(s), signature on page %d\nSigner: %s\nSHA-256: %s…\n\nThis is a legal signature. Check the document if you're not sure.",
		d.Name, d.Pages, d.Page, d.Signer, d.SHA256[:16])
	if d.Warn != "" {
		s = d.Warn + "\n\n" + s
	}
	return s
}

func pinText(d Document) string {
	s := fmt.Sprintf("Enter your token PIN to sign %s.\n\nWrong PINs count towards locking the token.", d.Name)
	if d.Warn != "" {
		s = d.Warn + "\n\n" + s
	}
	return s
}
