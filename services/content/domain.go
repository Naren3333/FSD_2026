package main

import (
	"errors"
	"fmt"
	"slices"
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

// Document statuses: an upload is indexing until ai-core's result is indexed or failed. Text files uploaded before
// they were indexed (docs/decisions/0013-native-document-formats.md) were extracted; migration 006 queues them.
const (
	statusExtracted = "extracted"
	statusIndexing  = "indexing"
	statusIndexed   = "indexed"
	statusFailed    = "failed"
)

// Media types Content accepts and serves.
const (
	mimePDF      = "application/pdf"
	mimePPTX     = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
	mimeMarkdown = "text/markdown"
	mimeText     = "text/plain"
	mimePNG      = "image/png"

	// pdfMagic starts every PDF; zipMagic every .pptx (a zip). extract needs only this much of either.
	pdfMagic = "%PDF-"
	zipMagic = "PK\x03\x04"
)

// indexable are the media types ai-core indexes: all that Content accepts.
var indexable = []string{mimePDF, mimePPTX, mimeMarkdown, mimeText}

const (
	maxFile  = 50 << 20 // a PDF or a deck
	maxText  = 5 << 20  // plain text or Markdown
	maxTitle = 160      // characters

	// uploadDeadline is how long one upload may take: a 50 MiB file at about 1.5 Mbit/s, matching the web app.
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
	// Set once indexing ends: indexed (searchable) or failed with a reason. A text file counts one page; a deck's
	// pages are its slides.
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

// extract checks an upload of the given media type. Only its first bytes are read for a PDF or a deck (data need
// hold no more than pdfMagic or zipMagic); a text file is read whole, to check it is UTF-8. Every accepted upload is
// indexed asynchronously, so it is indexing with no chunks of its own.
func extract(data []byte, kind string) ([]Chunk, string, error) {
	switch kind {
	case mimePDF:
		if !strings.HasPrefix(string(data), pdfMagic) {
			return nil, "", errors.New("file is not a PDF")
		}
	case mimePPTX:
		if !strings.HasPrefix(string(data), zipMagic) {
			return nil, "", errors.New("file is not a PowerPoint .pptx deck (a password-protected deck or an old .ppt can't be read)")
		}
	case mimeText, mimeMarkdown:
		if len(data) > maxText {
			return nil, "", errTooLarge
		}
		if !utf8.Valid(data) || strings.ContainsRune(string(data), 0) {
			return nil, "", errors.New("text files must be UTF-8")
		}
		if strings.TrimSpace(string(data)) == "" {
			return nil, "", errors.New("document is empty")
		}
	default:
		return nil, "", errors.New("upload a PDF, a PowerPoint .pptx deck, a Markdown .md file or UTF-8 plain text")
	}
	return []Chunk{}, statusIndexing, nil // the indexing worker sends it to ai-core
}

// isIndexable reports whether ai-core indexes documents of this media type.
func isIndexable(kind string) bool { return slices.Contains(indexable, kind) }
