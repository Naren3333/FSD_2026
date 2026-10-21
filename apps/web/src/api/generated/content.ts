// generated from contracts/http/content.openapi.json; run npm run contracts to refresh.

export type paths = Record<string, never>;
export type webhooks = Record<string, never>;
export interface components {
  schemas: {
    Chunk: {
      ordinal: number;
      text: string;
      start_character: number;
      end_character: number;
    };
    Document: {
      id: string;
      classroom_id: string;
      title: string;
      mime_type:
        | 'text/plain'
        | 'application/pdf'
        | 'text/markdown'
        | 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
        | 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';
      status: 'extracted' | 'extraction_unsupported' | 'indexing' | 'indexed' | 'failed';
      revision: number;
      sha256: string;
      doc_type: 'teaching_material' | 'rubric' | 'marking_scheme';
      chunks?: components['schemas']['Chunk'][];
      page_count?: number;
      chunk_count?: number;
      embedding_model?: string;
      indexed_at?: string;
      failure_reason?:
        | 'NO_TEXT_LAYER'
        | 'ENCRYPTED'
        | 'CORRUPT'
        | 'TOO_LARGE'
        | 'AI_UNAVAILABLE'
        | 'INTERNAL';
    };
    DocumentPage: {
      items: components['schemas']['Document'][];
      total: number;
    };
    Error: {
      error: {
        code: string;
        message: string;
      };
    };
    RelatedItem: {
      chunk_id: string;
      kind: 'table' | 'figure' | 'text';
      label: string | null;
      page_start: number;
      image: string | null;
      content: string;
    };
    SearchHit: {
      chunk_id: string;
      document_id: string;
      title: string;
      doc_type: 'teaching_material' | 'rubric' | 'marking_scheme';
      section_path: string | null;
      page_start: number;
      page_end: number;
      content: string;
      kind: 'text' | 'table' | 'figure';
      source: 'pdf' | 'vision' | 'markdown' | 'pptx' | 'docx';
      image: string | null;
      score: number;
      related: components['schemas']['RelatedItem'][];
    };
    SearchRequest: {
      classroom_id: string;
      query: string;
      top_k?: number;
      rerank?: boolean;
      document_ids?: string[];
      doc_types?: ('teaching_material' | 'rubric' | 'marking_scheme')[];
    };
    SearchResponse: {
      results: components['schemas']['SearchHit'][];
      rerank_fallback: boolean;
    };
  };
  responses: never;
  parameters: never;
  requestBodies: never;
  headers: never;
  pathItems: never;
}
export type $defs = Record<string, never>;
export type operations = Record<string, never>;
