// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { IdentityApi } from '../api/clients';
import type { Classroom } from '../api/models';
import { ClassroomContext } from './classroom-context';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const room = (id: string): Classroom => ({ id, name: `Class ${id}`, teacher_id: 't1' });

/* a promise and the functions that settle it, for holding a read open. */
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => ((resolve = yes), (reject = no)));
  return { promise, resolve, reject };
}

function setup(classrooms: () => Promise<Classroom[]>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [{ provide: IdentityApi, useValue: { classrooms: vi.fn(classrooms) } }],
  });
  return TestBed.inject(ClassroomContext);
}

describe('ClassroomContext', () => {
  it('is unknown until the first list loads, then keeps the current classroom while it is still there', async () => {
    const context = setup(() => Promise.resolve([room('a'), room('b')]));
    expect([context.known(), context.loading()]).toEqual([false, false]);
    const read = context.refresh();
    expect(context.loading()).toBe(true);
    await read;
    expect([context.known(), context.loading(), context.classroomId()]).toEqual([true, false, 'a']);
    context.select('b');
    await context.refresh();
    expect(context.classroomId()).toBe('b');
  });

  it('keeps the newest list when an older read answers last, such as one from before a deletion', async () => {
    const answers = [deferred<Classroom[]>(), deferred<Classroom[]>()];
    let call = 0;
    const context = setup(() => answers[call++].promise);
    const older = context.refresh();
    const newer = context.refresh();
    answers[1].resolve([room('b')]);
    await newer;
    answers[0].resolve([room('a'), room('b')]);
    await older;
    expect(context.classrooms()).toEqual([room('b')]);
    expect(context.classroomId()).toBe('b');
    expect(context.loading()).toBe(false);
  });

  it('stays loading while a newer read is pending, and its caller waits for that newer read', async () => {
    const answers = [deferred<Classroom[]>(), deferred<Classroom[]>()];
    let call = 0;
    const context = setup(() => answers[call++].promise);
    let olderDone = false;
    const older = context.refresh().then(() => (olderDone = true));
    const newer = context.refresh();
    answers[0].resolve([room('a')]);
    await Promise.resolve();
    await Promise.resolve();
    expect([context.loading(), context.known(), olderDone]).toEqual([true, false, false]);
    answers[1].resolve([room('b')]);
    await Promise.all([older, newer]);
    expect([context.loading(), context.classroomId(), olderDone]).toEqual([false, 'b', true]);
  });

  it('reports a failed read, ignores an older one failing late, and recovers on the next', async () => {
    const answers = [deferred<Classroom[]>(), deferred<Classroom[]>()];
    let call = 0;
    const context = setup(() => answers[call++].promise);
    const older = context.refresh();
    const newer = context.refresh();
    answers[1].resolve([room('a')]);
    answers[0].reject(new Error('Identity is unavailable.'));
    await Promise.all([older, newer]);
    expect(context.error()).toBe('');

    const failing = setup(() => Promise.reject(new Error('Identity is unavailable.')));
    await expect(failing.refresh()).rejects.toThrow('Identity is unavailable.');
    expect([failing.known(), failing.loading(), failing.error()]).toEqual([
      false,
      false,
      'Identity is unavailable.',
    ]);
  });
});
