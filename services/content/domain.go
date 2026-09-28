package main

import (
	"errors"
	"strings"
	"unicode/utf8"
)

type Chunk struct {
	Ordinal int    `json:"ordinal"`
	Text    string `json:"text"`
	Start   int    `json:"start_character"`
	End     int    `json:"end_character"`
}

func extract(data []byte, kind string) ([]Chunk, string, error) {
	if kind == "application/pdf" {
		if !strings.HasPrefix(string(data), "%PDF-") {
			return nil, "", errors.New("file is not a PDF")
		}
		return []Chunk{}, "extraction_unsupported", nil
	}
	if kind != "text/plain" || !utf8.Valid(data) || strings.ContainsRune(string(data), 0) {
		return nil, "", errors.New("upload UTF-8 plain text or a valid PDF")
	}
	runes := []rune(string(data))
	if len(runes) == 0 {
		return nil, "", errors.New("document is empty")
	}
	chunks := []Chunk{}
	for start := 0; start < len(runes); start += 1000 {
		end := start + 1000
		if end > len(runes) {
			end = len(runes)
		}
		chunks = append(chunks, Chunk{len(chunks), string(runes[start:end]), start, end})
	}
	return chunks, "extracted", nil
}
