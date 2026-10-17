// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import { PagerComponent, pageCount } from './pager';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

function render(page: number, total: number, disabled = false) {
  TestBed.resetTestingModule();
  TestBed.overrideComponent(PagerComponent, { set: { template: template('./pager.html') } });
  const fixture = TestBed.createComponent(PagerComponent);
  for (const [name, value] of Object.entries({
    label: 'All students pages',
    page,
    pageSize: 10,
    total,
    disabled,
  }))
    fixture.componentRef.setInput(name, value);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const went: number[] = [];
  fixture.componentInstance.go.subscribe((p) => went.push(p));
  const button = (name: string) =>
    [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === name) as HTMLButtonElement;
  return { root, button, went };
}

describe('pager', () => {
  it('counts at least one page', () => {
    expect(pageCount(0, 10)).toBe(1);
    expect(pageCount(10, 10)).toBe(1);
    expect(pageCount(11, 10)).toBe(2);
  });

  it('shows nothing when everything fits on one page', () => {
    expect(render(0, 10).root.querySelector('nav')).toBeNull();
  });

  it('names the page and total, and moves one page at a time within bounds', () => {
    const first = render(0, 25);
    expect(first.root.querySelector('nav')?.getAttribute('aria-label')).toBe('All students pages');
    expect(first.root.textContent).toContain('Page 1 of 3 · 25 students');
    expect(first.button('Previous').disabled).toBe(true);
    first.button('Next').click();
    expect(first.went).toEqual([1]);

    const last = render(2, 25);
    expect(last.button('Next').disabled).toBe(true);
    last.button('Previous').click();
    expect(last.went).toEqual([1]);
  });

  it('disables both controls while the list is busy', () => {
    const page = render(1, 25, true);
    expect(page.button('Previous').disabled).toBe(true);
    expect(page.button('Next').disabled).toBe(true);
  });
});
