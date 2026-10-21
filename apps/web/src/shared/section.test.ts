import { describe, expect, it } from 'vitest';
import { Section } from './section';

/* a promise and the functions that settle it, for holding a load open. */
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => ((resolve = yes), (reject = no)));
  return { promise, resolve, reject };
}

describe('Section', () => {
  it('is not ready while loading or after a failure, so an empty list is only shown once loaded', async () => {
    const section = new Section<string[]>();
    const answer = deferred<string[]>();
    const loading = section.load(() => answer.promise);
    expect([section.loading(), section.ready(), section.value()]).toEqual([true, false, null]);
    answer.resolve([]);
    await loading;
    expect([section.loading(), section.ready(), section.value()]).toEqual([false, true, []]);

    await section.load(() => Promise.reject(new Error('Assessment is unavailable.')));
    expect([section.ready(), section.error(), section.value()]).toEqual([
      false,
      'Assessment is unavailable.',
      null,
    ]);
  });

  it('lands only the newest request, and tells older work it is stale', async () => {
    const section = new Section<string>();
    const slow = deferred<string>();
    let stale: (() => boolean) | undefined;
    const first = section.load((current) => ((stale = current), slow.promise));
    await section.load(() => Promise.resolve('new classroom'));
    slow.resolve('old classroom');
    await first;
    expect(section.value()).toBe('new classroom');
    expect(stale?.()).toBe(false);
    expect(section.loading()).toBe(false);
  });

  it('shows a value set by a mutation in place of a pending load, and clears to nothing', async () => {
    const section = new Section<number>();
    const pending = deferred<number>();
    const load = section.load(() => pending.promise);
    section.set(2);
    expect([section.value(), section.loading()]).toEqual([2, false]);
    pending.resolve(1);
    await load;
    expect(section.value()).toBe(2);
    section.clear();
    expect([section.value(), section.ready()]).toEqual([null, true]);
  });

  it('polls quietly: no loading state, and a failure keeps what is on show', async () => {
    const section = new Section<number>();
    section.set(1);
    const poll = section.poll(() => Promise.resolve(2));
    expect(section.loading()).toBe(false);
    expect(await poll).toBe(true);
    expect(section.value()).toBe(2);
    expect(await section.poll(() => Promise.reject(new Error('offline')))).toBe(false);
    expect([section.value(), section.error()]).toEqual([2, '']);
  });

  it('reloads after a mutation only while nothing newer started, such as a load for another classroom', async () => {
    const section = new Section<string>();
    const reread = deferred<string>();
    const reload = section.reload(() => reread.promise);
    await section.load(() => Promise.resolve('classroom B'));
    reread.resolve('classroom A');
    await reload;
    expect([section.value(), section.loading()]).toEqual(['classroom B', false]);

    const pending = deferred<string>();
    const load = section.load(() => pending.promise);
    await section.reload(() => Promise.resolve('after the mutation'));
    pending.resolve('older load');
    await load;
    expect([section.value(), section.loading()]).toEqual(['after the mutation', false]);
    await expect(section.reload(() => Promise.reject(new Error('offline')))).rejects.toThrow('offline');
    expect([section.value(), section.loading(), section.error()]).toEqual(['after the mutation', false, '']);
  });

  it('reports a failed re-read that replaced a first load still under way, rather than show nothing as loaded', async () => {
    const section = new Section<string[]>();
    const first = deferred<string[]>();
    const load = section.load(() => first.promise);
    await expect(section.reload(() => Promise.reject(new Error('offline')))).rejects.toThrow('offline');
    first.resolve(['older load']);
    await load;
    expect([section.value(), section.loading(), section.ready(), section.error()]).toEqual([
      null,
      false,
      false,
      'offline',
    ]);
  });
});
