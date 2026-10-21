import { describe, expect, it } from 'vitest';
import { documentLimit, documentMime, isIndexable } from './files';

describe('native document selection', () => {
  it('maps extensions independently of the browser MIME guess', () => {
    expect(documentMime({ name: 'NOTES.MD' })).toBe('text/markdown');
    expect(documentMime({ name: 'notes.markdown' })).toBe('text/markdown');
    expect(documentMime({ name: 'slides.pptx' })).toContain('presentationml');
    expect(documentMime({ name: 'lesson.docx' })).toContain('wordprocessingml');
    expect(documentMime({ name: 'legacy.ppt' })).toBeUndefined();
    expect(isIndexable({ name: 'notes.md' })).toBe(true);
    expect(isIndexable({ name: 'notes.txt' })).toBe(false);
  });
  it('applies text and binary upload limits', () => {
    expect(documentLimit({ name: 'notes.md' })).toBe(5 * 1024 * 1024);
    expect(documentLimit({ name: 'slides.pptx' })).toBe(50 * 1024 * 1024);
  });
});
