import { describe, expect, it } from 'vitest';
import type { DocumentRecord } from '../api/models';
import { sourceDocuments } from './generation';

const doc = (change: Partial<DocumentRecord>): DocumentRecord => ({
  id: 'd1',
  classroom_id: 'c1',
  title: 'Testing slides',
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  sha256: 'a'.repeat(64),
  doc_type: 'teaching_material',
  ...change,
});

describe('source documents', () => {
  it('offers only indexed teaching materials, for generation and practice alike', () => {
    const docs = [
      doc({}),
      doc({ id: 'scheme', doc_type: 'marking_scheme' }),
      doc({ id: 'x', status: 'failed' }),
      doc({ id: 'busy', status: 'indexing' }),
      doc({ id: 'text', status: 'extracted', mime_type: 'text/plain' }),
    ];
    expect(sourceDocuments(docs).map((d) => d.id)).toEqual(['d1']);
  });
});
