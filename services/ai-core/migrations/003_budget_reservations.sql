-- per-operation reservations shared by all replicas.
CREATE TABLE budget_reservations (
 id uuid PRIMARY KEY,
 org_id text NOT NULL,
 user_id text NOT NULL,
 amount numeric(18,10) NOT NULL DEFAULT 0 CHECK(amount >= 0),
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '30 minutes'
);
CREATE INDEX budget_reservations_user ON budget_reservations(org_id,user_id);
