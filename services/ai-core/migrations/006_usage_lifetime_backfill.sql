-- adds the rows that predate the trigger. row share locks, not a table lock, keep ledger inserts running; an
-- update or delete of an older row waits, or this migration gives up its lock wait and retries.
DO $$
BEGIN
  -- a ledger that ran an earlier form of migration 005 was already backfilled.
  IF to_regclass('usage_lifetime_backfill') IS NOT NULL THEN
    UPDATE usage_lifetime_total
    SET cost_usd = cost_usd + (
      SELECT coalesce(sum(cost_usd), 0)
      FROM (
        SELECT cost_usd FROM usage WHERE id <= (SELECT through FROM usage_lifetime_backfill) FOR SHARE
      ) AS earlier
    )
    WHERE shard = 0;
    DROP TABLE usage_lifetime_backfill;
  END IF;
END;
$$;

CREATE OR REPLACE FUNCTION maintain_usage_lifetime_total() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  stripe smallint := pg_backend_pid() % 16;
BEGIN
  IF TG_OP = 'TRUNCATE' THEN
    UPDATE usage_lifetime_total SET cost_usd = 0;
  ELSIF TG_OP = 'INSERT' THEN
    UPDATE usage_lifetime_total SET cost_usd = cost_usd + NEW.cost_usd WHERE shard = stripe;
  ELSIF TG_OP = 'UPDATE' THEN
    UPDATE usage_lifetime_total SET cost_usd = cost_usd + NEW.cost_usd - OLD.cost_usd WHERE shard = stripe;
  ELSE
    UPDATE usage_lifetime_total SET cost_usd = cost_usd - OLD.cost_usd WHERE shard = stripe;
  END IF;
  RETURN NULL;
END;
$$;
