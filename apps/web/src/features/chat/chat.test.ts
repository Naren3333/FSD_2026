import { describe, it, expect } from 'vitest';
import type { Citation, DocumentRecord } from '../../api/models';
import { SseParser } from '../../core/sse';
import { describe as describeDocument } from '../materials/materials';
import { markdown, pages } from './markdown';

const A = '11111111-1111-4111-8111-111111111111',
  B = '22222222-2222-4222-8222-222222222222';
const cite = (id: string): Citation => ({
  chunk_id: id,
  document_id: id,
  title: 'Notes',
  page_start: 2,
  page_end: 3,
  kind: 'text',
  source: 'pdf',
  content: 'x',
  image: null,
});
const doc = (change: Partial<DocumentRecord>): DocumentRecord => ({
  id: A,
  classroom_id: B,
  title: 'T',
  mime_type: 'application/pdf',
  status: 'indexing',
  revision: 1,
  doc_type: 'teaching_material',
  ...change,
});

describe('SseParser', () => {
  it('joins frames split across reads and skips malformed ones', () => {
    const parser = new SseParser();
    expect(parser.push('event: token\ndata: {"te')).toEqual([]);
    expect(
      parser.push(
        'xt":"Hi"}\n\nevent: token\r\ndata: nope\r\n\r\nevent: done\ndata: {"outcome":"answered"}\n\n',
      ),
    ).toEqual([
      { event: 'token', data: { text: 'Hi' } },
      { event: 'done', data: { outcome: 'answered' } },
    ]);
  });
});

describe('markdown', () => {
  it('renders the answer subset as data, numbering citations by first use', () => {
    const text = `The materials cover:\n\n- **Processes:** how \`join()\` waits [doc:${B}].\n- *States* [doc:${A}][doc:${'3'.repeat(8)}-3333-4333-8333-333333333333]\n\n1. One\n2. Two`;
    expect(markdown(text, [cite(A), cite(B)])).toEqual([
      { kind: 'p', items: [[{ kind: 'text', text: 'The materials cover:', ref: 0 }]] },
      {
        kind: 'ul',
        items: [
          [
            { kind: 'strong', text: 'Processes:', ref: 0 },
            { kind: 'text', text: ' how ', ref: 0 },
            { kind: 'code', text: 'join()', ref: 0 },
            { kind: 'text', text: ' waits ', ref: 0 },
            { kind: 'ref', text: '', ref: 2 },
            { kind: 'text', text: '.', ref: 0 },
          ],
          [
            { kind: 'em', text: 'States', ref: 0 },
            { kind: 'text', text: ' ', ref: 0 },
            { kind: 'ref', text: '', ref: 1 },
          ],
        ],
      },
      {
        kind: 'ol',
        items: [[{ kind: 'text', text: 'One', ref: 0 }], [{ kind: 'text', text: 'Two', ref: 0 }]],
      },
    ]);
  });
  it('keeps identifiers and arithmetic literal, and hides a tag still streaming', () => {
    expect(markdown('Use snake_case_name and 2*3*4 [doc:1111', [])).toEqual([
      { kind: 'p', items: [[{ kind: 'text', text: 'Use snake_case_name and 2*3*4', ref: 0 }]] },
    ]);
  });
  it('keeps code blocks verbatim and headings as blocks', () => {
    expect(markdown('## Steps\n```\nThread t = new Thread();\n**x**\n```', [])).toEqual([
      { kind: 'h', items: [[{ kind: 'text', text: 'Steps', ref: 0 }]] },
      { kind: 'pre', items: [[{ kind: 'code', text: 'Thread t = new Thread();\n**x**', ref: 0 }]] },
    ]);
  });
  it('describes page ranges', () => {
    expect(pages({ page_start: 4, page_end: 4 })).toBe('page 4');
    expect(pages(cite(A))).toBe('pages 2–3');
    expect(pages({ page_start: 2, page_end: 2, source: 'pptx' })).toBe('slide 2');
    expect(
      pages({ page_start: 4, page_end: 4, source: 'markdown', section_path: 'Storage > Transactions' }),
    ).toBe('Storage > Transactions');
    expect(pages({ page_start: 1, page_end: 3, source: 'docx' })).toBe('Document text');
  });
});

describe('document status', () => {
  it('never presents extraction as indexing', () => {
    expect(describeDocument(doc({ status: 'extracted', mime_type: 'text/plain' })).detail).toMatch(
      /not indexed/,
    );
    expect(describeDocument(doc({ status: 'extraction_unsupported' })).label).toBe('Not indexed');
    expect(describeDocument(doc({ status: 'indexed', page_count: 12, chunk_count: 20 })).detail).toBe(
      '12 pages, 20 passages. Available to the assistant.',
    );
    expect(describeDocument(doc({ status: 'indexed', page_count: 1, chunk_count: 1 })).detail).toBe(
      '1 page, 1 passage. Available to the assistant.',
    );
    expect(describeDocument(doc({ status: 'failed', failure_reason: 'NO_TEXT_LAYER' })).detail).toMatch(
      /no readable text layer/,
    );
  });
});
