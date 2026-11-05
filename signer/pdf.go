package main

import (
	"bytes"
	"crypto"
	"crypto/x509"
	"errors"
	"fmt"
	"time"

	"github.com/digitorus/pdfsign"
	"github.com/digitorus/pdfsign/revocation"
)

// Box size the stamp is laid out for, in PDF points.
const (
	DefaultBoxWidth  = 220.0
	DefaultBoxHeight = 65.0
)

// DefaultTSA gives every signature a trusted time, so it still checks out after the certificate expires.
const DefaultTSA = "http://timestamp.digicert.com"

// Placement says where the signature box goes. X and Y are its bottom-left corner in points,
// measured from the bottom-left corner of the visible page.
type Placement struct {
	Page          int
	X, Y          float64
	Width, Height float64
}

type SignOptions struct {
	Placement Placement
	Image     []byte // optional handwritten signature, PNG or JPEG
	TSA       string
	Now       time.Time
}

// PageInfo describes one page of a PDF: its visible size and where that area starts.
type PageInfo struct {
	Width, Height    float64
	OriginX, OriginY float64
	Rotate           int
}

// Inspect returns the page count and the geometry of one page (1-based).
func Inspect(pdf []byte, page int) (int, PageInfo, error) {
	doc, err := pdfsign.Open(bytes.NewReader(pdf), int64(len(pdf)))
	if err != nil {
		return 0, PageInfo{}, err
	}
	rdr := doc.Reader()
	count := rdr.NumPage()
	if page < 1 || page > count {
		return count, PageInfo{}, fmt.Errorf("page %d doesn't exist: the PDF has %d", page, count)
	}
	v := rdr.Page(page).V
	box := inherited(v, "CropBox")
	if box.Len() != 4 {
		box = inherited(v, "MediaBox")
	}
	if box.Len() != 4 {
		return count, PageInfo{}, errors.New("this page has no size")
	}
	x0, y0, x1, y1 := box.Index(0).Float64(), box.Index(1).Float64(), box.Index(2).Float64(), box.Index(3).Float64()
	if x1 < x0 {
		x0, x1 = x1, x0
	}
	if y1 < y0 {
		y0, y1 = y1, y0
	}
	return count, PageInfo{
		Width: x1 - x0, Height: y1 - y0, OriginX: x0, OriginY: y0,
		Rotate: ((int(inherited(v, "Rotate").Int64()) % 360) + 360) % 360,
	}, nil
}

// SignPDF adds one visible signature as an incremental update, so the original bytes and any
// earlier signatures stay intact.
func SignPDF(in []byte, signer crypto.Signer, cert *x509.Certificate, chain []*x509.Certificate, o SignOptions) ([]byte, error) {
	p := o.Placement
	if p.Width <= 0 {
		p.Width = DefaultBoxWidth
	}
	if p.Height <= 0 {
		p.Height = DefaultBoxHeight
	}
	_, info, err := Inspect(in, p.Page)
	if err != nil {
		return nil, err
	}
	if info.Rotate != 0 {
		return nil, fmt.Errorf("page %d is rotated %d°, which isn't supported yet", p.Page, info.Rotate)
	}
	if p.X < 0 || p.Y < 0 || p.X+p.Width > info.Width || p.Y+p.Height > info.Height {
		return nil, fmt.Errorf("the signature box doesn't fit on page %d (%.0f × %.0f points)", p.Page, info.Width, info.Height)
	}

	doc, err := pdfsign.Open(bytes.NewReader(in), int64(len(in)))
	if err != nil {
		return nil, err
	}
	now := o.Now
	if now.IsZero() {
		now = time.Now()
	}
	app := pdfsign.NewAppearance(p.Width, p.Height)
	textX := p.Width * 4 / DefaultBoxWidth
	if len(o.Image) > 0 {
		imgW := p.Width * 0.54
		app.Image(doc.AddImage("signature", o.Image)).Rect(2, 2, imgW, p.Height-4).ScaleFit()
		textX = p.Width * 124.8 / DefaultBoxWidth
	}
	font := pdfsign.StandardFont(pdfsign.Courier) // the library writes flat glyph widths, which only suit a fixed-width font
	scale := p.Height / DefaultBoxHeight          // a resized box scales its text too
	mid := p.Height / 2
	for i, line := range []string{"Digitally signed by", cert.Subject.CommonName, "Date: " + now.Format("02 Jan 2006 15:04 MST")} {
		app.Text(line).Font(font, 6*scale).Position(textX, mid+(8-float64(i)*8)*scale)
	}

	tsa := o.TSA
	if tsa == "" {
		tsa = DefaultTSA
	}
	// PAdES B-T: trusted time, no revocation fetching. CCA's http:// revocation URLs can hang for
	// minutes and aren't needed for a valid signature.
	doc.Sign(signer, cert, chain...).
		Type(pdfsign.ApprovalSignature).
		Format(pdfsign.PAdES_B_T).
		Timestamp(tsa).
		RevocationFunction(func(_, _ *x509.Certificate, _ *revocation.InfoArchival) error { return nil }).
		Appearance(app, p.X+info.OriginX, p.Y+info.OriginY).
		Page(p.Page)

	var out bytes.Buffer
	if _, err := doc.Write(&out); err != nil {
		return nil, err
	}
	return out.Bytes(), nil
}
