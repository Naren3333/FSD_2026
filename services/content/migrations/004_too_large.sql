-- too_large: ai-core refuses a pdf over its page or chunk cap before paying for the rest of it.
ALTER TABLE documents DROP CONSTRAINT IF EXISTS documents_failure_reason_check;
ALTER TABLE documents ADD CONSTRAINT documents_failure_reason_check CHECK (failure_reason IN ('NO_TEXT_LAYER','ENCRYPTED','CORRUPT','TOO_LARGE','AI_UNAVAILABLE','INTERNAL'));
