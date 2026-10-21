-- Expand provenance formats without changing or deleting existing documents/chunks.
ALTER TABLE document_chunks DROP CONSTRAINT document_chunks_source_check;
ALTER TABLE document_chunks ADD CONSTRAINT document_chunks_source_check
    CHECK (source IN ('pdf','vision','markdown','pptx','docx'));
COMMENT ON COLUMN document_chunks.page_start IS
    '1-based PDF page, PPTX slide, or logical section anchor for Markdown/DOCX. Not a rendered page for unpaginated formats.';
COMMENT ON COLUMN document_chunks.page_end IS
    'Inclusive end of source-unit range; display according to source, never as pages for Markdown/DOCX.';
