import { describe, expect, it } from 'vitest';
import { SseParser } from './sse';

describe('SseParser', () => {
  it('emits nothing until a frame ends, however the text is split', () => {
    const parser = new SseParser();
    const stream = 'event: token\ndata: {"text":"Half"}\n\nevent: done\ndata: {"outcome":"answered"}\n\n';
    const events = [...stream].flatMap((char) => parser.push(char));
    expect(events).toEqual([
      { event: 'token', data: { text: 'Half' } },
      { event: 'done', data: { outcome: 'answered' } },
    ]);
  });

  it('holds a frame whose blank line is split between reads', () => {
    const parser = new SseParser();
    expect(parser.push('event: token\ndata: {"text":"a"}\n')).toEqual([]);
    expect(parser.push('\n')).toEqual([{ event: 'token', data: { text: 'a' } }]);
  });

  it('joins multi-line data with newlines before reading the JSON', () => {
    const parser = new SseParser();
    expect(parser.push('event: citations\ndata: {"citations":\ndata: [],\ndata: "n": 2}\n\n')).toEqual([
      { event: 'citations', data: { citations: [], n: 2 } },
    ]);
    // A newline inside a JSON string must arrive escaped; the line break between data lines is not part of it.
    expect(parser.push('data: {"text":\ndata: "line"}\n\n')).toEqual([
      { event: 'message', data: { text: 'line' } },
    ]);
  });

  it('ignores comments, ids, retry hints and frames with no data', () => {
    const parser = new SseParser();
    expect(
      parser.push(
        ': keep-alive\n\nid: 7\nretry: 1000\n\n: note\nevent: token\nid: 8\ndata: {"text":"x"}\n\n',
      ),
    ).toEqual([{ event: 'token', data: { text: 'x' } }]);
  });

  it('takes data with or without the space after the colon, and CR or CRLF line ends', () => {
    const parser = new SseParser();
    expect(parser.push('event:done\rdata:{"outcome":"no_results"}\r\rdata: {"a":1}\r\n\r\n')).toEqual([
      { event: 'done', data: { outcome: 'no_results' } },
      { event: 'message', data: { a: 1 } },
    ]);
  });

  it('reads a CRLF split between two reads as one line end, not a blank line', () => {
    const parser = new SseParser();
    expect(parser.push('event: token\r')).toEqual([]);
    expect(parser.push('\ndata: {"text":"a"}\r')).toEqual([]);
    // The CR that starts the blank line already ends the frame; the LF after it is not another blank line.
    expect(parser.push('\n\r')).toEqual([{ event: 'token', data: { text: 'a' } }]);
    expect(parser.push('\nevent: done\r\ndata: {"outcome":"answered"}\r\n\r\n')).toEqual([
      { event: 'done', data: { outcome: 'answered' } },
    ]);
  });

  it('skips a malformed frame and carries on with the next', () => {
    const parser = new SseParser();
    expect(parser.push('data: {broken\n\nevent: done\ndata: {"outcome":"error"}\n\n')).toEqual([
      { event: 'done', data: { outcome: 'error' } },
    ]);
  });
});
