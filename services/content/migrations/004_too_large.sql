-- TOO_LARGE: ai-core refuses a PDF over its page or chunk cap before paying for the rest of it. Additive: the
-- check only widens, so every stored row still satisfies it.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_failure_reason_check;
ALTER TABLE documents ADD CONSTRAINT documents_failure_reason_check CHECK (failure_reason IN ('NO_TEXT_LAYER','ENCRYPTED','CORRUPT','TOO_LARGE','AI_UNAVAILABLE','INTERNAL'));
