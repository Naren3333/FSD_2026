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

export function pages(c: Pick<Citation, 'page_start' | 'page_end'>): string {
  return c.page_start === c.page_end ? `page ${c.page_start}` : `pages ${c.page_start}–${c.page_end}`;
}
