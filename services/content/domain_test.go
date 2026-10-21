package main

import (
	"errors"
	"strings"
	"testing"
)

func TestEveryAcceptedUploadWaitsForTheIndexer(t *testing.T) {
	for _, c := range []struct{ kind, data string }{
		{mimePDF, "%PDF-1.7"},
		{mimePPTX, "PK\x03\x04"},
		{mimeMarkdown, "# Notes\n\nPhotosynthesis makes glucose."},
		{mimeText, strings.Repeat("é", 1001)},
	} {
		chunks, status, err := extract([]byte(c.data), c.kind)
		if err != nil || status != statusIndexing || len(chunks) != 0 {
			t.Errorf("%s: %v %s %v; every upload must wait for the indexing worker, never get made-up chunks", c.kind, chunks, status, err)
		}
		if !isIndexable(c.kind) {
			t.Errorf("%s is accepted but not indexable", c.kind)
		}
	}
}

func TestFilesMustBeWhatTheyClaim(t *testing.T) {
	for _, c := range []struct{ kind, data string }{
		{mimePDF, "not a pdf"},
		{mimePPTX, "%PDF-1.7"},
		{mimePPTX, "\xd0\xcf\x11\xe0"}, // a password-protected deck, or an old .ppt
		{"application/msword", "PK\x03\x04"},
	} {
		if _, _, err := extract([]byte(c.data), c.kind); err == nil {
			t.Errorf("%s: %q accepted", c.kind, c.data)
		}
	}
}

func TestTextLimit(t *testing.T) {
	for _, kind := range []string{mimeText, mimeMarkdown} {
		if _, _, err := extract([]byte(strings.Repeat("a", maxText+1)), kind); !errors.Is(err, errTooLarge) {
			t.Fatalf("%s over 5 MiB accepted", kind)
		}
	}
}

func TestUnsupportedFiles(t *testing.T) {
	for _, b := range [][]byte{{0xff}, {0}, {}, []byte(" \n\t")} {
		for _, kind := range []string{mimeText, mimeMarkdown} {
			if _, _, err := extract(b, kind); err == nil {
				t.Fatalf("invalid %s %q accepted", kind, b)
			}
		}
	}
}
