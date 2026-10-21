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

// documentfiles is what the handlers need from object storage: originals and figure crops, and removing an
// original whose database row was never committed.
type documentFiles interface {
	objects
	upload(ctx context.Context, key string, body io.Reader, size int64, contentType string) error
	removeOrphan(documentID, key string)
}

// storage is the s3-compatible bucket holding originals and figure crops.
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

// put stores a small object held in memory, such as a figure crop.
func (s storage) put(ctx context.Context, key string, data []byte, contentType string) error {
	return s.upload(ctx, key, bytes.NewReader(data), int64(len(data)), contentType)
}

// upload streams an original of known size into the bucket.
func (s storage) upload(ctx context.Context, key string, body io.Reader, size int64, contentType string) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, body, size, minio.PutObjectOptions{ContentType: contentType})
	return err
}

// removeorphan deletes an object whose database row was never committed.
func (s storage) removeOrphan(documentID, key string) {
	cleanup, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := s.client.RemoveObject(cleanup, s.bucket, key, minio.RemoveObjectOptions{}); err != nil {
		slog.Error("orphan_object_cleanup_failed", "document_id", documentID)
	}
}

// removeall deletes every object under prefix: a document's original and its figure crops.
func (s storage) removeAll(ctx context.Context, prefix string) error {
	ctx, cancel := context.WithCancel(ctx) // stops the listing if a removal fails part way
	defer cancel()
	for object := range s.client.ListObjects(ctx, s.bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true}) {
		if object.Err != nil {
			return object.Err
		}
		if err := s.client.RemoveObject(ctx, s.bucket, object.Key, minio.RemoveObjectOptions{}); err != nil {
			return err
		}
	}
	return nil
}

// list is every object in the bucket with when it was written, for the sweeper.
func (s storage) list(ctx context.Context) ([]storedObject, error) {
	ctx, cancel := context.WithCancel(ctx) // stops the listing if it fails part way
	defer cancel()
	var objects []storedObject
	for object := range s.client.ListObjects(ctx, s.bucket, minio.ListObjectsOptions{Recursive: true}) {
		if object.Err != nil {
			return nil, object.Err
		}
		objects = append(objects, storedObject{object.Key, object.LastModified})
	}
	return objects, nil
}

// modified is when the object was last written, with exists false once it is gone.
func (s storage) modified(ctx context.Context, key string) (time.Time, bool, error) {
	info, err := s.client.StatObject(ctx, s.bucket, key, minio.StatObjectOptions{})
	if minio.ToErrorResponse(err).Code == "NoSuchKey" {
		return time.Time{}, false, nil
	}
	if err != nil {
		return time.Time{}, false, err
	}
	return info.LastModified, true, nil
}

// remove deletes one object; removing one that is already gone succeeds.
func (s storage) remove(ctx context.Context, key string) error {
	return s.client.RemoveObject(ctx, s.bucket, key, minio.RemoveObjectOptions{})
}

// open returns the object stream and its size.
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
