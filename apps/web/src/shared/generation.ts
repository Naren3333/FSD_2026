import type { DocumentRecord } from '../api/models';

/* the question-generation agent's limits, for a teacher's drafts and a student's practice alike (adr 0006, 0022). */
export const MAX_SOURCE_DOCUMENTS = 10;
export const MAX_COUNT = 10;
export const MAX_FOCUS = 300;
export const MAX_EXISTING = 50;

/* what questions are generated from: the classroom's indexed teaching materials. */
export function sourceDocuments(documents: readonly DocumentRecord[]): DocumentRecord[] {
  return documents.filter((d) => d.doc_type === 'teaching_material' && d.status === 'indexed');
}
