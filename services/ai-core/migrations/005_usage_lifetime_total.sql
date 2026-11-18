-- striped so concurrent ledger writers on different connections do not queue on one counter row.
CREATE TABLE usage_lifetime_total (
  shard smallint PRIMARY KEY CHECK (shard BETWEEN 0 AND 15),
  cost_usd numeric NOT NULL
);

INSERT INTO usage_lifetime_total (shard, cost_usd) SELECT stripe, 0 FROM generate_series(0, 15) AS stripe;

-- rows up to this id predate the trigger; migration 006 adds them without holding a table lock.
CREATE TABLE usage_lifetime_backfill (through bigint NOT NULL);

-- held only until this short migration commits: once granted, every earlier ledger write has committed, so
-- each row with a larger id is counted by the trigger.
LOCK TABLE usage IN SHARE ROW EXCLUSIVE MODE;

INSERT INTO usage_lifetime_backfill SELECT coalesce(max(id), 0) FROM usage;

CREATE FUNCTION maintain_usage_lifetime_total() RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  stripe smallint := pg_backend_pid() % 16;
BEGIN
  IF TG_OP = 'TRUNCATE' THEN
    UPDATE usage_lifetime_total SET cost_usd = 0;
    UPDATE usage_lifetime_backfill SET through = 0;
  ELSIF TG_OP = 'INSERT' THEN
    UPDATE usage_lifetime_total SET cost_usd = cost_usd + NEW.cost_usd WHERE shard = stripe;
  ELSIF OLD.id <= (SELECT through FROM usage_lifetime_backfill) THEN
    -- the backfill reads this row's latest value.
    NULL;
  ELSIF TG_OP = 'UPDATE' THEN
    UPDATE usage_lifetime_total SET cost_usd = cost_usd + NEW.cost_usd - OLD.cost_usd WHERE shard = stripe;
  ELSE
    UPDATE usage_lifetime_total SET cost_usd = cost_usd - OLD.cost_usd WHERE shard = stripe;
  END IF;
  RETURN NULL;
END;
$$;

CREATE TRIGGER usage_lifetime_cost
AFTER INSERT OR UPDATE OF cost_usd OR DELETE ON usage
FOR EACH ROW EXECUTE FUNCTION maintain_usage_lifetime_total();

CREATE TRIGGER usage_lifetime_reset
AFTER TRUNCATE ON usage
FOR EACH STATEMENT EXECUTE FUNCTION maintain_usage_lifetime_total();
