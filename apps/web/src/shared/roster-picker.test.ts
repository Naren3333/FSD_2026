// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { IdentityApi } from '../api/clients';
import type { Student } from '../api/models';
import { PagerComponent } from './pager';
import { RosterPickerComponent } from './roster-picker';
import { SectionStateComponent } from './section-state';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const people: Student[] = Array.from({ length: 25 }, (_, i) => ({
  id: `s${i}`,
  username: `student${i}`,
  display_name: `Student ${i}`,
}));

/* the picker over a roster read twenty at a time; `chosen` follows what it emits, as a page binds it. */
function render(inputs: Record<string, unknown> = {}, roster: Student[] = people) {
  const rosterPage = vi.fn((_classroom: string, offset: number, limit: number) =>
    Promise.resolve({ students: roster.slice(offset, offset + limit), total: roster.length }),
  );
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [{ provide: IdentityApi, useValue: { rosterPage } }] });
  TestBed.overrideComponent(RosterPickerComponent, { set: { template: template('./roster-picker.html') } });
  TestBed.overrideComponent(PagerComponent, { set: { template: template('./pager.html') } });
  TestBed.overrideComponent(SectionStateComponent, { set: { template: template('./section-state.html') } });
  const fixture = TestBed.createComponent(RosterPickerComponent);
  const picker = fixture.componentInstance;
  const emitted: Map<string, string>[] = [];
  picker.chosenChange.subscribe((next) => {
    emitted.push(next);
    fixture.componentRef.setInput('chosen', next);
  });
  fixture.componentRef.setInput('classroomId', 'c1');
  for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
  const root = fixture.nativeElement as HTMLElement;
  const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const box = (name: string) =>
    [...root.querySelectorAll<HTMLLabelElement>('label.check')]
      .find((l) => l.querySelector('.resit-choice > span')?.textContent?.trim() === name)
      ?.querySelector('input') as HTMLInputElement | undefined;
  const tick = (name: string) => {
    const input = box(name)!;
    input.checked = !input.checked;
    input.dispatchEvent(new Event('change'));
    fixture.detectChanges();
  };
  const chips = () =>
    [...root.querySelectorAll('[aria-label="Chosen students"] button')].map((b) =>
      b.textContent?.replace(/\s+/g, ' ').trim(),
    );
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find(
      (b) => b.textContent?.replace(/\s+/g, ' ').trim() === name,
    ) as HTMLButtonElement;
  fixture.detectChanges();
  return { fixture, picker, root, emitted, settle, box, tick, chips, button, rosterPage };
}

describe('the roster picker', () => {
  it('reads twenty students a page and keeps names chosen on another page', async () => {
    const page = render();
    await page.settle();
    expect(page.rosterPage).toHaveBeenCalledWith('c1', 0, 20);
    expect(page.root.querySelectorAll('label.check').length).toBe(20);
    page.tick('Student 1');
    expect(page.emitted.at(-1)).toEqual(new Map([['s1', 'Student 1']]));
    page.button('Next').click();
    await page.settle();
    expect(page.rosterPage).toHaveBeenLastCalledWith('c1', 20, 20);
    expect(page.root.querySelectorAll('label.check').length).toBe(5);
    expect(page.chips()).toEqual(['Remove Student 1']);
    page.button('Remove Student 1').click();
    page.fixture.detectChanges();
    expect(page.emitted.at(-1)).toEqual(new Map());
    expect(page.chips()).toEqual([]);
  });

  it('holds at most `max` students, disabling the rest with why', async () => {
    const page = render({ max: 2 });
    await page.settle();
    page.tick('Student 1');
    page.tick('Student 2');
    expect(page.root.textContent).toContain('2 students are chosen, the most a resit can have.');
    expect(page.box('Student 3')?.disabled).toBe(true);
    expect(page.box('Student 1')?.disabled).toBe(false);
    page.picker.toggle(people[3], true);
    expect(page.emitted.length).toBe(2);
  });

  it('keeps locked students chosen, says why and offers no removal', async () => {
    const page = render({
      chosen: new Map([['s1', 'Student 1']]),
      locked: new Set(['s1']),
      lockedReason: 'Students who have submitted the resit stay on it.',
    });
    await page.settle();
    const box = page.box('Student 1')!;
    expect(box.checked).toBe(true);
    expect(box.disabled).toBe(true);
    const reason = page.root.querySelector('#roster-locked');
    expect(reason?.textContent).toContain('stay on it');
    expect(box.getAttribute('aria-describedby')).toBe('roster-locked');
    expect(page.chips()).toEqual(['Student 1 (submitted)']);
    page.picker.remove('s1');
    expect(page.emitted).toEqual([]);
  });

  it('names a chosen student on no page read from the fallback names, else by bare id', async () => {
    const page = render({
      chosen: new Map([
        ['left', ''],
        ['s1', ''],
        ['gone', ''],
      ]),
      names: new Map([['left', 'Lee Former']]),
    });
    await page.settle();
    expect(page.chips()).toEqual(['Remove Lee Former', 'Remove Student 1', 'Remove gone']);
    expect(page.root.querySelector('[aria-label="Chosen students"] .identifier')?.textContent).toBe('gone');
  });

  it('marks suggested students and their notes, and shows the empty and failed rosters', async () => {
    const page = render({
      suggested: new Set(['s1']),
      suggestedLabel: 'Lost marks',
      notes: new Map([['s1', '1 of 2 marks']]),
    });
    await page.settle();
    const row = page.box('Student 1')!.closest('label')!;
    expect(row.textContent).toContain('Lost marks');
    expect(row.textContent).toContain('1 of 2 marks');

    const empty = render({}, []);
    await empty.settle();
    expect(empty.root.textContent).toContain('No students are enrolled in this classroom yet.');

    const failing = render();
    failing.rosterPage.mockRejectedValueOnce(new Error('Identity is unavailable.'));
    failing.picker.load();
    await failing.settle();
    expect(failing.root.textContent).toContain('Identity is unavailable.');
    failing.button('Try again').click();
    await failing.settle();
    expect(failing.root.querySelectorAll('label.check').length).toBe(20);
  });
});
