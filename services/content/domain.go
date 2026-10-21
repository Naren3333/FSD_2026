package main

import (
	"bytes"
	"errors"
	"fmt"
	"strings"
	"time"
	"unicode/utf8"
)

// Document types. Teachers upload teaching material only. Rubrics and marking schemes uploaded before they were
// retired (docs/decisions/0009-retire-marking-scheme-uploads.md) keep their type and stay with teachers: students
// never see them.
const (
	docTeachingMaterial = "teaching_material"
	docRubric           = "rubric"
	docMarkingScheme    = "marking_scheme"
)

var docTypes = map[string]bool{docTeachingMaterial: true, docRubric: true, docMarkingScheme: true}

// Document statuses: a text file is extracted on upload; an indexable document is indexing until ai-core's result is indexed or
// failed.
const (
	statusExtracted = "extracted"
	statusIndexing  = "indexing"
	statusIndexed   = "indexed"
	statusFailed    = "failed"
)

// Media types Content accepts and serves.
const (
	mimePDF      = "application/pdf"
	mimeText     = "text/plain"
	mimeMarkdown = "text/markdown"
	mimePPTX     = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
	mimeDOCX     = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
	mimePNG      = "image/png"

	// pdfMagic starts every PDF; extract needs only this much of one.
	pdfMagic = "%PDF-"
)

const (
	maxPDF   = 50 << 20
	maxText  = 5 << 20
	maxTitle = 160 // characters

	// uploadDeadline is how long one upload may take: a 50 MiB PDF at about 1.5 Mbit/s, matching the web app.
	uploadDeadline = 5 * time.Minute
	// removeDeadline is how long deleting a document's objects and row may take.
	removeDeadline = time.Minute
	// downloadDeadline is how long sending an original or a figure may take, on the same reckoning.
	downloadDeadline = uploadDeadline
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
	// Set for indexable documents once indexing ends: indexed (searchable) or failed with a reason.
	PageCount      *int       `json:"page_count,omitempty"`
	ChunkCount     *int       `json:"chunk_count,omitempty"`
	EmbeddingModel *string    `json:"embedding_model,omitempty"`
	IndexedAt      *time.Time `json:"indexed_at,omitempty"`
	FailureReason  *string    `json:"failure_reason,omitempty"`
}

// newDocument is an upload about to be stored: the document plus who owns it and where its original is.
type newDocument struct {
	Document
	Org, Owner, Key string
}

// storedDocument is a document with the object key of its original.
type storedDocument struct {
	Document
	Key string
}

type Chunk struct {
	Ordinal int    `json:"ordinal"`
	Text    string `json:"text"`
	Start   int    `json:"start_character"`
	End     int    `json:"end_character"`
}

var errTooLarge = fmt.Errorf("text documents must be no larger than %d MiB", maxText>>20)

// extract chunks a text document. A PDF is only checked here, by its first bytes (data need hold no more than
// pdfMagic): it is indexed asynchronously.
func extract(data []byte, kind string) ([]Chunk, string, error) {
	if kind == mimePDF {
		if !strings.HasPrefix(string(data), pdfMagic) {
			return nil, "", errors.New("file is not a PDF")
		}
		return []Chunk{}, statusIndexing, nil // the indexing worker sends it to ai-core
	}
	if kind == mimePPTX || kind == mimeDOCX {
		if !bytes.HasPrefix(data, []byte("PK\x03\x04")) {
			return nil, "", errors.New("file is not an Office Open XML package")
		}
		return []Chunk{}, statusIndexing, nil
	}
	if len(data) > maxText {
		return nil, "", errTooLarge
	}
	if (kind != mimeText && kind != mimeMarkdown) || !utf8.Valid(data) || strings.ContainsRune(string(data), 0) {
		return nil, "", errors.New("upload UTF-8 text/Markdown, a valid PDF, or a PPTX/DOCX package")
	}
	runes := []rune(string(data))
	if len(runes) == 0 {
		return nil, "", errors.New("document is empty")
	}
	chunks := []Chunk{}
	if kind == mimeMarkdown {
		return chunks, statusIndexing, nil
	}
	for start := 0; start < len(runes); start += 1000 {
		end := min(start+1000, len(runes))
		chunks = append(chunks, Chunk{len(chunks), string(runes[start:end]), start, end})
	}
	return chunks, statusExtracted, nil
}

func indexable(kind string) bool {
	return kind == mimePDF || kind == mimeMarkdown || kind == mimePPTX || kind == mimeDOCX
}
