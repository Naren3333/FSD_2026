-- the topics of a message about several (student_tutor.v5, teacher.v4): per passage in the prompt, keyed by chunk
-- id, {"primary": the topic it is grouped under, "topics": every topic search that found it}. chunk ids and
-- cleaned topic labels only, never passage text, so a deleted document's redaction (adr 0010) needs nothing here.
-- null for single-topic turns and earlier answers.
-- additive only.
ALTER TABLE messages ADD COLUMN IF NOT EXISTS passage_topics jsonb;
