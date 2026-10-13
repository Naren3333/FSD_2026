package main

import (
	"errors"
	"strings"
	"time"
	"unicode/utf8"
)

// Document types. Students see teaching material only; rubrics and marking schemes stay with teachers.
const (
	docTeachingMaterial = "teaching_material"
	docRubric           = "rubric"
	docMarkingScheme    = "marking_scheme"
)

var docTypes = map[string]bool{docTeachingMaterial: true, docRubric: true, docMarkingScheme: true}

// Document statuses: a text file is extracted on upload; a PDF is indexing until ai-core's result is indexed or
// failed.
const (
	statusExtracted = "extracted"
	statusIndexing  = "indexing"
	statusIndexed   = "indexed"
	statusFailed    = "failed"
)

// Media types Content accepts and serves.
const (
	mimePDF  = "application/pdf"
	mimeText = "text/plain"
	mimePNG  = "image/png"
)

const (
	maxPDF  = 50 << 20
	maxText = 5 << 20
)

type Document struct {
	ID        string  `json:"id"`
	Classroom string  `json:"classroom_id"`
	Title     string  `json:"title"`
	Type      string  `json:"mime_type"`
	Status    string  `json:"status"`
	Revision  int     `json:"revision"`
	SHA       string  `json:"sha256"`
	DocType   string  `json:"doc_type"`
	Chunks    []Chunk `json:"chunks,omitempty"`
	// Set for PDFs once indexing ends: indexed (searchable) or failed with a reason.
	PageCount      *int       `json:"page_count,omitempty"`
	ChunkCount     *int       `json:"chunk_count,omitempty"`
	EmbeddingModel *string    `json:"embedding_model,omitempty"`
	IndexedAt      *time.Time `json:"indexed_at,omitempty"`
	FailureReason  *string    `json:"failure_reason,omitempty"`
}

type Chunk struct {
	Ordinal int    `json:"ordinal"`
	Text    string `json:"text"`
	Start   int    `json:"start_character"`
	End     int    `json:"end_character"`
}

var errTooLarge = errors.New("text documents must be no larger than 5 MiB")

// extract chunks a text document. A PDF is only checked here: it is indexed asynchronously.
func extract(data []byte, kind string) ([]Chunk, string, error) {
	if kind == mimePDF {
		if !strings.HasPrefix(string(data), "%PDF-") {
			return nil, "", errors.New("file is not a PDF")
		}
		return []Chunk{}, statusIndexing, nil // the indexing worker sends it to ai-core
	}
	if len(data) > maxText {
		return nil, "", errTooLarge
	}
	if kind != mimeText || !utf8.Valid(data) || strings.ContainsRune(string(data), 0) {
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
	return chunks, statusExtracted, nil
}
