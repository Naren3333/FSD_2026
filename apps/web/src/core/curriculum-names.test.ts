// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { CurriculumApi } from '../api/clients';
import type { Profile, Topic } from '../api/models';
import { CurriculumNames } from './curriculum-names';
import { Session } from './session';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const topic = (id: string, name: string): Topic => ({ id, name });

function setup() {
  const profile = signal<Pick<Profile, 'id'> | null>({ id: 'teacher-1' });
  let curriculum = [topic('k1', 'Fractions')];
  const archived = [topic('k0', 'Halves')];
  let reads = 0;
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Session, useValue: { profile } },
      {
        provide: CurriculumApi,
        useValue: {
          topicNames: (ids: string[]) => (
            reads++,
            Promise.resolve([...curriculum, ...archived].filter((t) => ids.includes(t.id)))
          ),
          topics: () => (reads++, Promise.resolve(curriculum)),
          archivedTopics: () => Promise.resolve(archived),
        },
      },
    ],
  });
  return {
    names: TestBed.inject(CurriculumNames),
    profile,
    reads: () => reads,
    add: (topic: Topic) => (curriculum = [...curriculum, topic]),
  };
}

describe('CurriculumNames', () => {
  it('shares overlapping reads and retries failures without retaining another user’s data', async () => {
    const t = setup();
    await Promise.all([t.names.need(['k1']), t.names.need(['k1'])]);
    expect(t.reads()).toBe(1);
    const api = TestBed.inject(CurriculumApi);
    const spy = vi.spyOn(api, 'topicNames').mockRejectedValueOnce(new Error('offline'));
    await expect(t.names.need(['k2'])).rejects.toThrow('offline');
    t.add(topic('k2', 'Decimals'));
    await t.names.need(['k2']);
    expect(t.names.topicName('k2')).toBe('Decimals');
    expect(spy).toHaveBeenCalledTimes(2);
    let finish!: (topics: Topic[]) => void;
    spy.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const pending = t.names.need(['k3']);
    await Promise.resolve();
    await Promise.resolve();
    t.profile.set({ id: 'student-1' });
    finish([topic('k3', 'Old user')]);
    await pending;
    expect(t.names.topicName('k3')).toBe('');
  });

  it('reads the curriculum once, and again only for a topic it does not know', async () => {
    const t = setup();
    await t.names.need(['k1']);
    await t.names.need(['k1']);
    expect(t.reads()).toBe(1);
    expect(t.names.topicName('k1')).toBe('Fractions');
    t.add(topic('k2', 'Decimals'));
    await t.names.need(['k1', 'k2']);
    expect(t.reads()).toBe(2);
    expect(t.names.topicName('k2')).toBe('Decimals');
    expect(t.names.topicName('gone')).toBe('');
  });

  it('batches requested names, caches misses and bounds retained entries', async () => {
    const t = setup();
    for (let i = 0; i < 600; i++) t.add(topic(`k${i + 10}`, `Topic ${i}`));
    await t.names.need(Array.from({ length: 600 }, (_, i) => `k${i + 10}`));
    expect(t.reads()).toBe(12);
    expect(t.names.topicName('k10')).toBe('');
    expect(t.names.topicName('k609')).toBe('Topic 599');
    await t.names.need(['gone']);
    await t.names.need(['gone']);
    expect(t.reads()).toBe(13);
  });

  it('names archived topics without offering them as active ones', async () => {
    const t = setup();
    await t.names.need(['k0']);
    expect(t.reads()).toBe(1);
    expect(t.names.topicName('k0')).toBe('Halves');
    expect(t.names.topics()).toEqual([]);
    expect(t.names.archived()).toEqual([]);
  });

  it('shares what the curriculum page reads, and never shows one user the names read for another', async () => {
    const t = setup();
    await t.names.refresh();
    t.add(topic('k2', 'Decimals'));
    await t.names.refresh();
    expect(t.names.topics().map((s) => s.name)).toEqual(['Fractions', 'Decimals']);
    t.profile.set({ id: 'student-1' });
    expect(t.names.topicName('k1')).toBe('');
    await t.names.need(['k1']);
    expect(t.reads()).toBe(3);
    expect(t.names.topicName('k1')).toBe('Fractions');
  });
});
