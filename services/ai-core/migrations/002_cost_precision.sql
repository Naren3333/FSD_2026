-- Widens cost_usd from 6 to 10 decimal places so a call cheaper than $0.0000005 (a short embedding or rerank) is
-- not stored as 0. The integer part keeps its 6 digits, so every existing value fits unchanged.
ALTER TABLE usage ALTER COLUMN cost_usd TYPE numeric(16,10);
