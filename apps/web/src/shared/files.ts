import type { WritableSignal } from '@angular/core';
import { messageOf } from './errors';

/* choose an explicit mime type from the supported extension; browser mime guesses vary by os. */
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

/* whether a chosen file is a pdf, by its name. */
export function isPdf(file: { name: string }): boolean {
  return /\.pdf$/i.test(file.name);
}

/* opens a document's original in a new tab, at `fragment`: the tab opens before the read so it isn't blocked as
a pop-up, and the file's url is revoked a minute later. */
export function openOriginal(
  read: () => Promise<string>,
  error: WritableSignal<string>,
  fragment = '',
): void {
  const tab = window.open('', '_blank');
  read()
    .then((url) => {
      if (tab) tab.location.href = url + fragment;
      else error.set('Allow pop-ups for this site to open the document.');
      setTimeout(() => URL.revokeObjectURL(url), 60000);
    })
    .catch((failure: unknown) => {
      tab?.close();
      error.set(`The document could not be opened. ${messageOf(failure, '')}`.trim());
    });
}
