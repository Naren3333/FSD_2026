import { signal } from '@angular/core';
import { Section } from './section';

export interface ListPage<T> {
  items: T[];
  total: number;
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
    let page = this.page();
    let result = await fetch(page * this.pageSize, this.pageSize);
    if (!result.items.length && result.total && page > 0) {
      page = Math.max(0, Math.ceil(result.total / this.pageSize) - 1);
      result = await fetch(page * this.pageSize, this.pageSize);
    }
    if (read === this.read) {
      this.total.set(result.total);
      this.page.set(page);
    }
    return result.items;
  }
}
