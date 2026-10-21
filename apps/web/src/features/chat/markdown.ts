import type { Citation } from '../../api/models';

/* one run of answer text: plain, bold, italic, code, or (ref > 0) the number of a cited source. */
export interface Inline {
  kind: 'text' | 'strong' | 'em' | 'code' | 'ref';
  text: string;
  ref: number;
}
/* a block of an answer. */
export interface Block {
  kind: 'p' | 'h' | 'ul' | 'ol' | 'pre';
  items: Inline[][];
}

const INLINE =
  /(\*\*[^*\n]+?\*\*|(?<!\w)__[^_\n]+?__(?!\w)|`[^`\n]+`|\[doc:[0-9a-f-]{36}\]|(?<![\w*])\*[^*\s][^*\n]*?\*(?![\w*])|(?<!\w)_[^_\s][^_\n]*?_(?!\w))/g;
const BULLET = /^\s*[-*•]\s+(.*)$/,
  NUMBERED = /^\s*\d+[.)]\s+(.*)$/,
  HEADING = /^\s*#{1,6}\s+(.*)$/;

/* the model's markdown as plain data for the template; nothing is ever inserted as html. */
export function markdown(text: string, citations: Citation[]): Block[] {
  const numbers = new Map(citations.map((c, i) => [c.chunk_id, i + 1]));
  const inline = (s: string) => inlines(s, numbers);
  const blocks: Block[] = [];
  const lines = text
    .replace(/\r\n?/g, '\n')
    .replace(/\[doc:[0-9a-f-]{0,36}$/, '')
    .split('\n');
  let paragraph: string[] = [];
  const flush = () => {
    if (paragraph.length) blocks.push({ kind: 'p', items: [inline(paragraph.join(' '))] });
    paragraph = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim().startsWith('```')) {
      flush();
      const code: string[] = [];
      while (++i < lines.length && !lines[i].trim().startsWith('```')) code.push(lines[i]);
      blocks.push({ kind: 'pre', items: [[{ kind: 'code', text: code.join('\n'), ref: 0 }]] });
      continue;
    }
    const heading = HEADING.exec(line),
      bullet = BULLET.exec(line),
      numbered = NUMBERED.exec(line);
    if (!line.trim()) flush();
    else if (heading) {
      flush();
      blocks.push({ kind: 'h', items: [inline(heading[1])] });
    } else if (bullet || numbered) {
      flush();
      const kind = bullet ? 'ul' : 'ol',
        item = inline((bullet || numbered)![1]);
      const last = blocks[blocks.length - 1];
      if (last?.kind === kind) last.items.push(item);
      else blocks.push({ kind, items: [item] });
    } else {
      const last = blocks[blocks.length - 1];
      // a wrapped line directly under a list item continues it.
      if (
        !paragraph.length &&
        (last?.kind === 'ul' || last?.kind === 'ol') &&
        /^\s/.test(line) &&
        lines[i - 1]?.trim()
      )
        last.items[last.items.length - 1].push(...inline(' ' + line.trim()));
      else paragraph.push(line.trim());
    }
  }
  flush();
  return blocks;
}

function inlines(s: string, numbers: Map<string, number>): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  const text = (t: string) => {
    if (t) out.push({ kind: 'text', text: t, ref: 0 });
  };
  for (const m of s.matchAll(INLINE)) {
    text(s.slice(last, m.index));
    last = m.index + m[0].length;
    const token = m[0];
    if (token.startsWith('[doc:')) {
      const ref = numbers.get(token.slice(5, -1));
      if (ref) out.push({ kind: 'ref', text: '', ref });
    } else if (token.startsWith('`')) out.push({ kind: 'code', text: token.slice(1, -1), ref: 0 });
    else if (token.startsWith('**') || token.startsWith('__'))
      out.push({ kind: 'strong', text: token.slice(2, -2), ref: 0 });
    else out.push({ kind: 'em', text: token.slice(1, -1), ref: 0 });
  }
  text(s.slice(last));
  return out;
}

/* a citation's page or page range, as the source list shows it. */
export function pages(
  c: Pick<Citation, 'page_start' | 'page_end'> & Partial<Pick<Citation, 'source' | 'section_path'>>,
): string {
  if (c.source === 'markdown' || c.source === 'docx') return c.section_path || 'Document text';
  const unit = c.source === 'pptx' ? 'slide' : 'page';
  return c.page_start === c.page_end ? `${unit} ${c.page_start}` : `${unit}s ${c.page_start}–${c.page_end}`;
}
