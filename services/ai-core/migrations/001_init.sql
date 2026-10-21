-- ai-core's own database (adr 0003): what each user's model calls cost.
CREATE TABLE IF NOT EXISTS usage(
  id bigserial PRIMARY KEY,
  org_id text NOT NULL,
  user_id text NOT NULL,
  role text NOT NULL CHECK(role IN ('student','teacher')),
  operation text NOT NULL CHECK(operation IN ('embed','generate','stream','rerank','ingest')),
  model text NOT NULL,
  tokens_in integer NOT NULL CHECK(tokens_in >= 0),
  tokens_out integer NOT NULL CHECK(tokens_out >= 0),
  cost_usd numeric(12,6) NOT NULL CHECK(cost_usd >= 0),
  estimated boolean NOT NULL DEFAULT false,
  correlation_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_by_user_day ON usage(org_id, user_id, created_at);
