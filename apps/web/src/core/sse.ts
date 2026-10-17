import type { StreamEvent } from '../api/models';

/** Splits a server-sent event stream into events; text may arrive in any pieces. */
export class SseParser {
  private buffer = '';
  /** The last piece ended in CR, so an LF starting the next one is the rest of that CRLF, not a blank line. */
  private afterCr = false;
  push(text: string): StreamEvent[] {
    if (this.afterCr && text.startsWith('\n')) text = text.slice(1);
    if (text) this.afterCr = text.endsWith('\r');
    this.buffer += text.replace(/\r\n?/g, '\n');
    const events: StreamEvent[] = [];
    let end: number;
    while ((end = this.buffer.indexOf('\n\n')) >= 0) {
      const frame = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      let event = 'message';
      const data: string[] = [];
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''));
      }
      if (!data.length) continue;
      try {
        events.push({ event, data: JSON.parse(data.join('\n')) as Record<string, unknown> });
      } catch {
        /* a malformed frame is skipped */
      }
    }
    return events;
  }
}
