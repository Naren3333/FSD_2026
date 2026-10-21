-- chat threads and messages, ported from fsd_test's assistant (adr 0002).
CREATE TABLE IF NOT EXISTS threads (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id          text        NOT NULL,
  user_id         text        NOT NULL,
  role            text        NOT NULL CHECK (role IN ('student','teacher')),
  classroom_id    uuid        NOT NULL,
  title           text,                                    -- the first message, cut to 80 characters
  created_at      timestamptz NOT NULL DEFAULT now(),
  last_message_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS threads_user_idx ON threads (org_id, user_id, last_message_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id           uuid        NOT NULL REFERENCES threads (id) ON DELETE CASCADE,
  role                text        NOT NULL CHECK (role IN ('user','assistant')),
  content             text        NOT NULL,
  -- assistant only:
  outcome             text        CHECK (outcome IN ('answered','no_results','error','interrupted')),
  rewritten_query     text,
  is_on_topic         boolean,
  retrieved_chunk_ids uuid[],                              -- every chunk put in the prompt
  citations           jsonb,                               -- only the chunks the answer cites
  prompt_version      text,
  model               text,
  usage_tokens        integer     CHECK (usage_tokens >= 0),
  latency_ms          integer     CHECK (latency_ms >= 0),
  created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK ((role = 'assistant') = (outcome IS NOT NULL)),
  CHECK (citations IS NULL OR jsonb_typeof(citations) = 'array')
);
CREATE INDEX IF NOT EXISTS messages_thread_idx ON messages (thread_id, created_at, id);
