package main

import (
	"crypto"
	"crypto/rsa"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/miekg/pkcs11"
)

// Errors the UI explains to the person. None of them is ever retried automatically, because a
// retry with a wrong PIN counts towards locking the token.
var (
	ErrNoDriver  = errors.New("the token driver isn't installed")
	ErrNoToken   = errors.New("no signing token found: plug it in and try again")
	ErrNoCert    = errors.New("no usable signing certificate on the token")
	ErrWrongPIN  = errors.New("wrong PIN: this attempt counted towards locking the token")
	ErrPINLocked = errors.New("the token's PIN is locked: your certifying authority has to unblock it")
)

// TokenInfo is what can be read without the PIN.
type TokenInfo struct {
	Label, Serial string
	Signer        string
	NotAfter      time.Time
	PINCountLow   bool
	PINFinalTry   bool
	PINLocked     bool
}

// Token is the signing device. Sign unlocks it with the PIN only for the duration of fn.
type Token interface {
	Info() (TokenInfo, error)
	Sign(pin string, fn func(crypto.Signer, *x509.Certificate) error) error
}

type P11Token struct{ Lib string }

// FindDriver returns the first PKCS#11 library that exists. An explicit path always wins.
func FindDriver(explicit string) (string, error) {
	if explicit != "" {
		if _, err := os.Stat(explicit); err != nil {
			return "", fmt.Errorf("%w: %s", ErrNoDriver, explicit)
		}
		return explicit, nil
	}
	if env := os.Getenv("LINOTYPE_PKCS11"); env != "" {
		return FindDriver(env)
	}
	home, _ := os.UserHomeDir()
	var patterns []string
	switch runtime.GOOS {
	case "darwin":
		patterns = []string{"/usr/local/lib/libcastle_v2*.dylib", filepath.Join(home, "hyp2003/libcastle_v2*.dylib")}
	case "linux":
		patterns = []string{filepath.Join(home, ".local/lib/hyp2003-fips/libcastle_v2.so*"), "/usr/lib/libcastle_v2.so*", "/usr/local/lib/libcastle_v2.so*"}
	case "windows":
		patterns = []string{`C:\Windows\System32\*castle*.dll`, `C:\Windows\System32\*hyp2003*.dll`}
	}
	for _, p := range patterns {
		if m, _ := filepath.Glob(p); len(m) > 0 {
			return m[0], nil
		}
	}
	return "", ErrNoDriver
}

type p11Session struct {
	ctx     *pkcs11.Ctx
	session pkcs11.SessionHandle
	info    pkcs11.TokenInfo
	cert    *x509.Certificate
	keyID   []byte
}

func (t P11Token) open() (*p11Session, func(), error) {
	ctx := pkcs11.New(t.Lib)
	if ctx == nil {
		return nil, nil, fmt.Errorf("%w: couldn't load %s", ErrNoDriver, t.Lib)
	}
	if err := ctx.Initialize(); err != nil {
		ctx.Destroy()
		return nil, nil, err
	}
	finish := func() { _ = ctx.Finalize(); ctx.Destroy() }
	slots, err := ctx.GetSlotList(true)
	if err != nil || len(slots) == 0 {
		finish()
		return nil, nil, ErrNoToken
	}
	info, err := ctx.GetTokenInfo(slots[0])
	if err != nil {
		finish()
		return nil, nil, err
	}
	session, err := ctx.OpenSession(slots[0], pkcs11.CKF_SERIAL_SESSION)
	if err != nil {
		finish()
		return nil, nil, err
	}
	s := &p11Session{ctx: ctx, session: session, info: info}
	done := func() { _ = ctx.CloseSession(session); finish() }
	if err := s.findCert(); err != nil {
		done()
		return nil, nil, err
	}
	return s, done, nil
}

// findCert picks the signing certificate that stays valid the longest.
func (s *p11Session) findCert() error {
	tmpl := []*pkcs11.Attribute{pkcs11.NewAttribute(pkcs11.CKA_CLASS, pkcs11.CKO_CERTIFICATE)}
	if err := s.ctx.FindObjectsInit(s.session, tmpl); err != nil {
		return err
	}
	objs, _, err := s.ctx.FindObjects(s.session, 16)
	_ = s.ctx.FindObjectsFinal(s.session)
	if err != nil {
		return err
	}
	now := time.Now()
	for _, o := range objs {
		attrs, err := s.ctx.GetAttributeValue(s.session, o, []*pkcs11.Attribute{
			pkcs11.NewAttribute(pkcs11.CKA_VALUE, nil), pkcs11.NewAttribute(pkcs11.CKA_ID, nil)})
		if err != nil {
			continue
		}
		c, err := x509.ParseCertificate(attrs[0].Value)
		if err != nil || c.KeyUsage&(x509.KeyUsageDigitalSignature|x509.KeyUsageContentCommitment) == 0 {
			continue
		}
		if now.Before(c.NotBefore) || now.After(c.NotAfter) {
			continue
		}
		if s.cert == nil || c.NotAfter.After(s.cert.NotAfter) {
			s.cert, s.keyID = c, attrs[1].Value
		}
	}
	if s.cert == nil {
		return ErrNoCert
	}
	return nil
}

func (t P11Token) Info() (TokenInfo, error) {
	s, done, err := t.open()
	if err != nil {
		return TokenInfo{}, err
	}
	defer done()
	return TokenInfo{
		Label: s.info.Label, Serial: s.info.SerialNumber,
		Signer: s.cert.Subject.CommonName, NotAfter: s.cert.NotAfter,
		PINCountLow: s.info.Flags&pkcs11.CKF_USER_PIN_COUNT_LOW != 0,
		PINFinalTry: s.info.Flags&pkcs11.CKF_USER_PIN_FINAL_TRY != 0,
		PINLocked:   s.info.Flags&pkcs11.CKF_USER_PIN_LOCKED != 0,
	}, nil
}

func (t P11Token) Sign(pin string, fn func(crypto.Signer, *x509.Certificate) error) error {
	s, done, err := t.open()
	if err != nil {
		return err
	}
	defer done()
	if s.info.Flags&pkcs11.CKF_USER_PIN_LOCKED != 0 {
		return ErrPINLocked
	}
	if err := s.ctx.Login(s.session, pkcs11.CKU_USER, pin); err != nil {
		var ck pkcs11.Error
		if errors.As(err, &ck) {
			switch uint(ck) {
			case pkcs11.CKR_PIN_INCORRECT, pkcs11.CKR_PIN_LEN_RANGE:
				return ErrWrongPIN
			case pkcs11.CKR_PIN_LOCKED:
				return ErrPINLocked
			}
		}
		return err
	}
	defer func() { _ = s.ctx.Logout(s.session) }()
	key, err := s.findKey()
	if err != nil {
		return err
	}
	return fn(&p11Signer{s: s, key: key}, s.cert)
}

func (s *p11Session) findKey() (pkcs11.ObjectHandle, error) {
	tmpl := []*pkcs11.Attribute{
		pkcs11.NewAttribute(pkcs11.CKA_CLASS, pkcs11.CKO_PRIVATE_KEY),
		pkcs11.NewAttribute(pkcs11.CKA_ID, s.keyID),
	}
	if err := s.ctx.FindObjectsInit(s.session, tmpl); err != nil {
		return 0, err
	}
	objs, _, err := s.ctx.FindObjects(s.session, 1)
	_ = s.ctx.FindObjectsFinal(s.session)
	if err != nil {
		return 0, err
	}
	if len(objs) == 0 {
		return 0, ErrNoCert
	}
	return objs[0], nil
}

// DER DigestInfo prefixes (RFC 8017 §9.2). CKM_RSA_PKCS pads what it's given, so the prefix has
// to be added here.
var digestInfo = map[crypto.Hash][]byte{
	crypto.SHA256: {0x30, 0x31, 0x30, 0x0d, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01, 0x05, 0x00, 0x04, 0x20},
	crypto.SHA384: {0x30, 0x41, 0x30, 0x0d, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x02, 0x05, 0x00, 0x04, 0x30},
	crypto.SHA512: {0x30, 0x51, 0x30, 0x0d, 0x06, 0x09, 0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x03, 0x05, 0x00, 0x04, 0x40},
}

type p11Signer struct {
	s   *p11Session
	key pkcs11.ObjectHandle
}

func (k *p11Signer) Public() crypto.PublicKey { return k.s.cert.PublicKey }

func (k *p11Signer) Sign(_ io.Reader, digest []byte, opts crypto.SignerOpts) ([]byte, error) {
	if _, ok := k.s.cert.PublicKey.(*rsa.PublicKey); !ok {
		return nil, errors.New("only RSA tokens are supported")
	}
	prefix, ok := digestInfo[opts.HashFunc()]
	if !ok {
		return nil, fmt.Errorf("unsupported digest %v", opts.HashFunc())
	}
	mech := []*pkcs11.Mechanism{pkcs11.NewMechanism(pkcs11.CKM_RSA_PKCS, nil)}
	if err := k.s.ctx.SignInit(k.s.session, mech, k.key); err != nil {
		return nil, err
	}
	return k.s.ctx.Sign(k.s.session, append(append([]byte{}, prefix...), digest...))
}
