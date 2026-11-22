-- how each recorded call ended and how long it took (adr 0023). both stay null on earlier rows and on
-- ingest charges; adding nullable columns rewrites nothing.
ALTER TABLE usage ADD COLUMN latency_ms integer, ADD COLUMN outcome text;

-- not valid: checked on new rows only, so existing rows are not scanned under the table lock.
ALTER TABLE usage ADD CONSTRAINT usage_outcome_known
  CHECK (outcome IN ('ok', 'rejected', 'timeout', 'failed')) NOT VALID;
