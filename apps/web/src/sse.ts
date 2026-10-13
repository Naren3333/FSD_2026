import type { Citation, StreamEvent } from './api';

/** Splits a server-sent event stream into events; text may arrive in any pieces. */
export class SseParser {
  private buffer = '';
  push(text: string): StreamEvent[] {
    this.buffer += text.replace(/\r\n?/g, '\n');
    const events: StreamEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf('\n\n')) >= 0) {
      const frame = this.buffer.slice(0, end); this.buffer = this.buffer.slice(end + 2);
      let event = 'message'; const data: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      }
      if (!data.length) continue;
      try { events.push({event, data: JSON.parse(data.join('\n')) as Record<string, unknown>}); } catch { /* a malformed frame is skipped */ }
    }
    return events;
  }
}

/** Text, or (ref > 0) the number of a cited source. */
export interface Segment { text: string; ref: number }
const TAG = /\[doc:([0-9a-f-]{36})\]/g;

/** An answer's text with each `[doc:<chunk_id>]` replaced by the number of its citation. A tag without a citation
 * (the answer was interrupted before the citations arrived) is dropped rather than shown as a raw identifier. */
export function segments(text: string, citations: Citation[]): Segment[] {
  const numbers = new Map(citations.map((c, i) => [c.chunk_id, i + 1]));
  const out: Segment[] = []; let last = 0;
  for (const match of text.matchAll(TAG)) {
    const before = text.slice(last, match.index);
    if (before) out.push({text: before, ref: 0});
    const ref = numbers.get(match[1]);
    if (ref) out.push({text: '', ref});
    last = match.index + match[0].length;
  }
  const rest = text.slice(last).replace(/\[doc:[0-9a-f-]{0,36}$/, ''); // a tag still arriving
  if (rest) out.push({text: rest, ref: 0});
  return out;
}

export function pages(c: Pick<Citation, 'page_start' | 'page_end'>): string {
  return c.page_start === c.page_end ? `page ${c.page_start}` : `pages ${c.page_start}–${c.page_end}`;
}
