// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it } from 'vitest';
import { CurriculumApi } from '../api/clients';
import type { Node, Profile } from '../api/models';
import { CurriculumNames } from './curriculum-names';
import { Session } from './session';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const skill = (id: string, name: string): Node => ({ id, kind: 'skill', name, parent_id: null });

function setup() {
  const profile = signal<Pick<Profile, 'id'> | null>({ id: 'teacher-1' });
  let curriculum = [skill('k1', 'Fractions')];
  let reads = 0;
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Session, useValue: { profile } },
      { provide: CurriculumApi, useValue: { nodes: () => (reads++, Promise.resolve(curriculum)) } },
    ],
  });
  return {
    names: TestBed.inject(CurriculumNames),
    profile,
    reads: () => reads,
    add: (node: Node) => (curriculum = [...curriculum, node]),
  };
}

describe('CurriculumNames', () => {
  it('reads the curriculum once, and again only for a skill it does not know', async () => {
    const t = setup();
    await t.names.need(['k1']);
    await t.names.need(['k1']);
    expect(t.reads()).toBe(1);
    expect(t.names.skillName('k1')).toBe('Fractions');
    t.add(skill('k2', 'Decimals'));
    await t.names.need(['k1', 'k2']);
    expect(t.reads()).toBe(2);
    expect(t.names.skillName('k2')).toBe('Decimals');
    expect(t.names.skillName('gone')).toBe('');
  });

  it('shares what the curriculum page reads, and never shows one user the names read for another', async () => {
    const t = setup();
    await t.names.refresh();
    t.add(skill('k2', 'Decimals'));
    await t.names.refresh();
    expect(t.names.skills().map((s) => s.name)).toEqual(['Fractions', 'Decimals']);
    t.profile.set({ id: 'student-1' });
    expect(t.names.skillName('k1')).toBe('');
    await t.names.need([]);
    expect(t.reads()).toBe(3);
    expect(t.names.skillName('k1')).toBe('Fractions');
  });
});
