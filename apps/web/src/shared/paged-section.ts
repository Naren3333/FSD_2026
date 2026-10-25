import { signal } from '@angular/core';
import { Section } from './section';

export interface ListPage<T> {
  items: T[];
  total: number;
}

/* reads page `page` of `size`; one past the end, after items were removed, falls back to the last page. */
export async function readPage<R extends { total: number }>(
  page: number,
  size: number,
  fetch: (offset: number, limit: number) => Promise<R>,
  isEmpty: (result: R) => boolean,
): Promise<{ result: R; page: number }> {
  let result = await fetch(page * size, size);
  if (isEmpty(result) && result.total && page > 0) {
    page = Math.max(0, Math.ceil(result.total / size) - 1);
    result = await fetch(page * size, size);
  }
  return { result, page };
}

/* page metadata follows the same newest-request rule as section. */
export class PagedSection<T> extends Section<T[]> {
  readonly page = signal(0);
  readonly total = signal(0);
  readonly pageSize = 20;
  private read = 0;

  override clear(): void {
    this.read++;
    super.clear();
  }

  async readPage(fetch: (offset: number, limit: number) => Promise<ListPage<T>>): Promise<T[]> {
    const read = ++this.read;
    const { result, page } = await readPage(this.page(), this.pageSize, fetch, (r) => !r.items.length);
    if (read === this.read) {
      this.total.set(result.total);
      this.page.set(page);
    }
    return result.items;
  }
}
