package main

import "context"

// EmbeddingVersion prevents mixing incompatible source revisions, chunkers or models.
// No implementation is registered in this milestone.
type EmbeddingVersion struct {
	Model            string
	Dimensions       int
	Chunker          string
	DocumentRevision int
}
type AuthorizedRetrievalRequest struct {
	DocumentIDs []string
	Question    string
	BearerToken string
}
type SourceEvidence struct {
	DocumentID string
	Revision   int
	SHA256     string
	Chunk      Chunk
}
type EmbeddingIndexer interface {
	Index(context.Context, string, EmbeddingVersion) error
}

// Retrieval must resolve authorization through the Content API before accessing chunks.
type AuthorizedRetriever interface {
	Retrieve(context.Context, AuthorizedRetrievalRequest) ([]SourceEvidence, error)
}
