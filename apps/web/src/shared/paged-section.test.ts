import { describe, expect, it } from 'vitest';
import { PagedSection } from './paged-section';

describe('paged sections', () => {
  it('falls back after the last item of the last page is deleted', async () => {
    const section = new PagedSection<number>();
    section.page.set(2);
    const asked: number[] = [];
    await section.load(() =>
      section.readPage(async (offset) => {
        asked.push(offset);
        return { items: offset === 40 ? [] : [21], total: 21 };
      }),
    );
    expect(asked).toEqual([40, 20]);
    expect(section.page()).toBe(1);
    expect(section.total()).toBe(21);
    expect(section.value()).toEqual([21]);
  });

  it('ignores an old classroom response for both rows and pagination metadata', async () => {
    const section = new PagedSection<number>();
    let finish!: (value: { items: number[]; total: number }) => void;
    const old = section.load(() =>
      section.readPage(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      ),
    );
    section.clear();
    await section.load(() => section.readPage(async () => ({ items: [2], total: 1 })));
    finish({ items: [1], total: 200 });
    await old;
    expect(section.value()).toEqual([2]);
    expect(section.total()).toBe(1);
  });
});
