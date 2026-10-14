package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"learning/platform"
	"log/slog"
	"time"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// documentFiles is what the handlers need from object storage: originals and figure crops, and removing an
// original whose database row was never committed.
type documentFiles interface {
	objects
	upload(ctx context.Context, key string, body io.Reader, size int64, contentType string) error
	removeOrphan(documentID, key string)
}

// storage is the S3-compatible bucket holding originals and figure crops.
type storage struct {
	client *minio.Client
	bucket string
}

func newStorage() (storage, error) {
	client, err := minio.New(platform.Required("S3_ENDPOINT"), &minio.Options{Creds: credentials.NewStaticV4(platform.Required("S3_ACCESS_KEY"), platform.Required("S3_SECRET_KEY"), ""), Secure: platform.Env("S3_SECURE", "true") == "true"})
	if err != nil {
		return storage{}, err
	}
	return storage{client, platform.Required("S3_BUCKET")}, nil
}

// check is the readiness probe: the document bucket must exist.
func (s storage) check(ctx context.Context) error {
	exists, err := s.client.BucketExists(ctx, s.bucket)
	if err != nil {
		return err
	}
	if !exists {
		return fmt.Errorf("document bucket missing")
	}
	return nil
}

func (s storage) put(ctx context.Context, key string, data []byte, contentType string) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, bytes.NewReader(data), int64(len(data)), minio.PutObjectOptions{ContentType: contentType})
	return err
}

// upload streams an original of known size into the bucket.
func (s storage) upload(ctx context.Context, key string, body io.Reader, size int64, contentType string) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, body, size, minio.PutObjectOptions{ContentType: contentType})
	return err
}

// removeOrphan deletes an object whose database row was never committed.
func (s storage) removeOrphan(documentID, key string) {
	cleanup, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.client.RemoveObject(cleanup, s.bucket, key, minio.RemoveObjectOptions{}); err != nil {
		slog.Error("orphan_object_cleanup_failed", "document_id", documentID)
	}
}

// open returns the object stream and its size. The caller must Close it.
func (s storage) open(ctx context.Context, key string) (io.ReadCloser, int64, error) {
	object, err := s.client.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, 0, err
	}
	info, err := object.Stat()
	if err != nil {
		object.Close()
		return nil, 0, err
	}
	return object, info.Size, nil
}
