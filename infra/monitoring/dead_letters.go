package main

import (
	"context"
	"math"
	"sync"
	"time"

	"github.com/segmentio/kafka-go"
)

func (c *collector) checkTopics(ctx context.Context, prior []topicStatus, at time.Time) []topicStatus {
	rows := append([]topicStatus(nil), prior...)
	for i := range rows {
		rows[i].CheckedAt = &at
		rows[i].State = "unavailable"
		if rows[i].LastSuccessAt != nil {
			rows[i].State = "stale"
		}
	}
	metadata, err := c.kafka.Metadata(ctx, &kafka.MetadataRequest{Topics: deadLetterTopics})
	if err != nil {
		return rows
	}
	byName := make(map[string]kafka.Topic, len(metadata.Topics))
	for _, topic := range metadata.Topics {
		byName[topic.Name] = topic
	}
	var work sync.WaitGroup
	for i := range rows {
		work.Go(func() {
			if count, ok := c.retained(ctx, byName[rows[i].Name]); ok {
				rows[i].Retained = &count
				rows[i].State = "available"
				rows[i].LastSuccessAt = &at
			}
		})
	}
	work.Wait()
	return rows
}

func (c *collector) retained(ctx context.Context, topic kafka.Topic) (int64, bool) {
	if topic.Error != nil || len(topic.Partitions) == 0 || len(topic.Partitions) > 128 {
		return 0, false
	}
	requests := make([]kafka.OffsetRequest, 0, len(topic.Partitions)*2)
	for _, partition := range topic.Partitions {
		if partition.Error != nil || partition.Leader.Host == "" {
			return 0, false
		}
		requests = append(requests, kafka.FirstOffsetOf(partition.ID), kafka.LastOffsetOf(partition.ID))
	}
	offsets, err := c.kafka.ListOffsets(ctx, &kafka.ListOffsetsRequest{
		Topics: map[string][]kafka.OffsetRequest{topic.Name: requests},
	})
	if err != nil {
		return 0, false
	}
	return sumOffsetSpans(offsets.Topics[topic.Name], len(topic.Partitions))
}

func sumOffsetSpans(offsets []kafka.PartitionOffsets, partitions int) (int64, bool) {
	if len(offsets) != partitions {
		return 0, false
	}
	var count int64
	for _, partition := range offsets {
		first, last := partition.FirstOffset, partition.LastOffset
		if partition.Error != nil || first < 0 || last < first || last-first > math.MaxInt64-count {
			return 0, false
		}
		count += last - first
	}
	return count, true
}
