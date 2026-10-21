-- the answer each user has in progress: one at a time, so parallel questions from several tabs can't
-- exhaust the user's ai-core concurrency slots.
CREATE TABLE IF NOT EXISTS active_turns(
  user_id text PRIMARY KEY,
  thread_id uuid NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  started_at timestamptz NOT NULL DEFAULT now()
);
