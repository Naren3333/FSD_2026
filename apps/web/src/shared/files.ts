/** Choose an explicit MIME type from the supported extension; browser MIME guesses vary by OS. */
export const DOCUMENT_MIMES: Record<string, string> = {
  pdf: 'application/pdf',
  txt: 'text/plain',
  md: 'text/markdown',
  markdown: 'text/markdown',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
};
export function documentMime(file: { name: string }): string | undefined {
  return DOCUMENT_MIMES[file.name.split('.').pop()?.toLowerCase() || ''];
}
export function isIndexable(file: { name: string }): boolean {
  const mime = documentMime(file);
  return !!mime && mime !== 'text/plain';
}
export function documentLimit(file: { name: string }): number {
  return ['text/plain', 'text/markdown'].includes(documentMime(file) || '')
    ? 5 * 1024 * 1024
    : 50 * 1024 * 1024;
}

/** Whether a chosen file is a PDF, by its name. */
export function isPdf(file: { name: string }): boolean {
  return /\.pdf$/i.test(file.name);
}
