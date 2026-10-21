import { computed, signal } from '@angular/core';
import type { ActionState } from './action-state';
import { messageOf } from './errors';

/* one part of a page that loads on its own: whether it is loading, its value, or the error that stopped it. */
export class Section<T> {
  readonly value = signal<T | null>(null);
  readonly loading = signal(false);
  readonly error = signal('');
  /* neither loading nor failed: the value, or its absence, is what there is to show. */
  readonly ready = computed(() => !this.loading() && !this.error());
  private request = 0;

  /* loads the value. `work` can ask whether its request is still the newest before it changes anything else. */
  async load(work: (current: () => boolean) => Promise<T>): Promise<void> {
    const request = ++this.request;
    const current = () => request === this.request;
    this.loading.set(true);
    this.error.set('');
    try {
      const value = await work(current);
      if (current()) this.value.set(value);
    } catch (error) {
      if (!current()) return;
      this.value.set(null);
      this.error.set(messageOf(error, 'This could not be loaded. Retry later.'));
    } finally {
      if (current()) this.loading.set(false);
    }
  }

  /* re-reads the value without showing it as loading. */
  async poll(work: () => Promise<T>): Promise<boolean> {
    const request = this.request;
    try {
      const value = await work();
      if (request === this.request && !this.loading()) this.value.set(value);
      return true;
    } catch {
      return false;
    }
  }

  /* re-reads the value after a mutation, in place of any pending load. */
  async reload(work: () => Promise<T>): Promise<void> {
    const request = ++this.request;
    try {
      const value = await work();
      if (request !== this.request) return;
      this.value.set(value);
      this.error.set('');
    } catch (error) {
      if (request === this.request && this.value() === null)
        this.error.set(messageOf(error, 'This could not be loaded. Retry later.'));
      throw error;
    } finally {
      if (request === this.request) this.loading.set(false);
    }
  }

  /* shows a value the page already has, such as a list it just changed locally, in place of any pending load. */
  set(value: T): void {
    this.request++;
    this.value.set(value);
    this.loading.set(false);
    this.error.set('');
  }

  clear(): void {
    this.request++;
    this.value.set(null);
    this.loading.set(false);
    this.error.set('');
  }
}

/* re-reads a list with `reread` after the item `id` was deleted from it. */
export async function rereadAfterDelete<T extends { id: string }>(
  list: Section<T[]>,
  id: string,
  reread: () => Promise<unknown>,
  current: () => boolean,
  state: ActionState,
  what: { list: string; refresh: string },
): Promise<void> {
  try {
    await reread();
  } catch (error) {
    const value = list.value();
    if (value && current()) list.set(value.filter((item) => item.id !== id));
    state.error.set(
      `The ${what.list} could not be refreshed. ${messageOf(error, `Choose ${what.refresh} to try again.`)}`,
    );
  }
}
