import type { DocumentRecord } from '../api/models';

export type MimeType = DocumentRecord['mime_type'];

const MIB = 1024 * 1024;

/** The files teachers can upload (ADR 0013), by extension: media type, upload limit and the name people know. */
export const FORMATS: readonly { extensions: string[]; type: MimeType; limit: number; label: string }[] = [
  { extensions: ['.pdf'], type: 'application/pdf', limit: 50 * MIB, label: 'PDF' },
  {
    extensions: ['.pptx'],
    type: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    limit: 50 * MIB,
    label: 'PowerPoint',
  },
  { extensions: ['.md', '.markdown'], type: 'text/markdown', limit: 5 * MIB, label: 'Markdown' },
  { extensions: ['.txt'], type: 'text/plain', limit: 5 * MIB, label: 'Text' },
];

/** The extensions the upload picker offers. */
export const ACCEPT = FORMATS.flatMap((f) => f.extensions).join(',');

/** The format of a chosen file, by its name, or undefined when the app doesn't accept it. */
export function formatOf(file: { name: string }): (typeof FORMATS)[number] | undefined {
  const name = file.name.toLowerCase();
  return FORMATS.find((f) => f.extensions.some((e) => name.endsWith(e)));
}

/** The name people know a stored document's format by. */
export function formatLabel(type: MimeType): string {
  return FORMATS.find((f) => f.type === type)?.label ?? type;
}

/** Where a passage's page numbers point: a PDF's pages, a deck's slides, or (a text file) nowhere. */
export function pageUnit(type: MimeType | undefined): 'page' | 'slide' | null {
  if (type === 'text/plain' || type === 'text/markdown') return null;
  return type === 'application/vnd.openxmlformats-officedocument.presentationml.presentation'
    ? 'slide'
    : 'page';
}
