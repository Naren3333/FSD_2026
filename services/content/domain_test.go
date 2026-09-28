package main

import (
	"strings"
	"testing"
)

func TestChunksHaveRealProvenance(t *testing.T) {
	text := strings.Repeat("é", 1001)
	chunks, status, err := extract([]byte(text), "text/plain")
	if err != nil || status != "extracted" || len(chunks) != 2 || chunks[1].Start != 1000 || chunks[1].End != 1001 || chunks[0].Text+chunks[1].Text != text {
		t.Fatalf("incorrect extraction: %v %s %v", chunks, status, err)
	}
}
func TestPDFHonestStatus(t *testing.T) {
	chunks, status, err := extract([]byte("%PDF-1.7"), "application/pdf")
	if err != nil || status != "extraction_unsupported" || len(chunks) != 0 {
		t.Fatal("fabricated extraction")
	}
}
func TestUnsupportedFiles(t *testing.T) {
	for _, b := range [][]byte{{0xff}, {0}, {}} {
		if _, _, err := extract(b, "text/plain"); err == nil {
			t.Fatal("invalid text accepted")
		}
	}
}
