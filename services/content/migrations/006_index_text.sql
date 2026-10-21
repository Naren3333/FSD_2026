-- Text files uploaded before ADR 0013 were stored but never indexed ('extracted'); ai-core now indexes them, so
-- each is queued as a teacher's reindex would queue it.
INSERT INTO index_jobs(document_id) SELECT id FROM documents WHERE status='extracted' AND mime_type='text/plain'
ON CONFLICT (document_id) DO NOTHING;
UPDATE documents SET status='indexing' WHERE status='extracted' AND mime_type='text/plain';
