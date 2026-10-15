import { signal } from '@angular/core';

/** One part of a page that loads on its own: whether it is loading, its value, or the error that stopped it. Only the newest
 * request lands, so a slow answer for a classroom the user has left never replaces the current one. */
export class Section<T> {
  readonly value = signal<T | null>(null);
  readonly loading = signal(false);
  readonly error = signal('');
  private request = 0;

  async load(work: () => Promise<T>): Promise<void> {
    const request = ++this.request;
    this.loading.set(true);
    this.error.set('');
    try {
      const value = await work();
      if (request === this.request) this.value.set(value);
    } catch (error) {
      if (request !== this.request) return;
      this.value.set(null);
      this.error.set(error instanceof Error ? error.message : 'This could not be loaded. Retry later.');
    } finally {
      if (request === this.request) this.loading.set(false);
    }
  }

  clear(): void {
    this.request++;
    this.value.set(null);
    this.loading.set(false);
    this.error.set('');
  }
}
