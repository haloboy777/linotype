# linotype-signer

Lets [pdf.ayushsingh.dev](https://pdf.ayushsingh.dev) sign PDFs with a USB token (Indian Class 3 DSC, PKCS#11). It runs on your own computer, because a web page can't talk to a token.

```
website ── PDF ──► linotype-signer on 127.0.0.1 ──► native window: review, PIN ──► token signs ──► signed PDF
```

The PIN and private key never reach the website. Signatures are PAdES B-T with a trusted timestamp, added as an incremental update, so earlier signatures stay valid.

## Use

```
linotype-signer doctor   # reads the token without a PIN
linotype-signer          # serve on 127.0.0.1:47823
```

Then press **✍ Sign** in Linotype and click where the signature goes. Chrome may ask once to allow access to apps on this device.

| Flag | Default | |
|---|---|---|
| `-lib` | known driver locations, or `$LINOTYPE_PKCS11` | PKCS#11 driver |
| `-origin` | `https://pdf.ayushsingh.dev` | websites allowed to ask, comma-separated |
| `-port` | `47823` | The Linotype page expects this one |
| `-tsa` | `http://timestamp.digicert.com` | timestamp server |

To stamp a handwritten signature, put a transparent PNG at `<config dir>/linotype-signer/signature.png`. `doctor` prints the path.

## Safety

- Requests must come from an allowed `Origin` and be addressed to the loopback `Host`. Anything else gets 403, including requests with no `Origin`.
- Every signature needs approval in a native window, which offers **View document** on the exact bytes to be signed, then asks for the PIN.
- The signer computes the hash from the whole PDF. It never signs a hash a website hands it.
- The PIN is sent to the token once. A wrong PIN is never retried, and a locked token is never tried.
- The token is logged out and released after each signature.

## Build

```
go test . && go build -o dist/linotype-signer .
```

It needs cgo, because PKCS#11 drivers are C libraries, so build on each OS. `.github/workflows/signer.yml` does that for macOS, Linux and Windows. Linux prompts need `zenity`.
