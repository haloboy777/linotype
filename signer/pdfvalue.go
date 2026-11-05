package main

import pdflib "github.com/digitorus/pdf"

// inherited reads a page attribute that may live on any parent in the page tree.
func inherited(page pdflib.Value, key string) pdflib.Value {
	v := page
	for i := 0; i < 64 && !v.IsNull(); i++ {
		if got := v.Key(key); !got.IsNull() {
			return got
		}
		v = v.Key("Parent")
	}
	return pdflib.Value{}
}
