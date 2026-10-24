// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import type { Usage } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { UsageComponent } from './usage';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
afterEach(() => document.body.replaceChildren());

const today = (spent: number): Usage =>
  ({ spent_usd: spent, limit_usd: 10, resets_at: '2026-10-25T00:00:00Z' }) as Usage;

/* the meter for this usage, in the document so focus can move. */
function render(usage: Usage | null, failed = false) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: AiUsage,
        useValue: {
          usage: signal(usage),
          loading: signal(false),
          failed: signal(failed),
          refresh: () => undefined,
        },
      },
    ],
  });
  TestBed.overrideComponent(UsageComponent, { set: { template: template('./usage.html') } });
  const fixture = TestBed.createComponent(UsageComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  document.body.append(root);
  const pill = root.querySelector('.usage-pill') as HTMLButtonElement;
  const popover = root.querySelector('#usage-popover') as HTMLElement;
  const text = (element: Element) => element.textContent?.replace(/\s+/g, ' ').trim() ?? '';
  return { fixture, root, pill, popover, text };
}

describe('the ai meter', () => {
  it('shows today’s spend on the pill, with the meter hidden from screen readers', () => {
    const page = render(today(1.8));
    expect(page.text(page.pill)).toBe('AI today $1.80 of $10.00');
    expect(page.pill.querySelector('meter')?.getAttribute('aria-hidden')).toBe('true');
    expect(page.pill.getAttribute('aria-expanded')).toBe('false');
    expect(page.popover.hidden).toBe(true);
  });

  it('opens the details on press and closes them on Escape, focusing the pill', () => {
    const page = render(today(1.8));
    page.pill.click();
    page.fixture.detectChanges();
    expect(page.pill.getAttribute('aria-expanded')).toBe('true');
    expect(page.popover.hidden).toBe(false);
    expect(page.popover.querySelector('meter')?.getAttribute('aria-labelledby')).toBe(
      'usage-label usage-text',
    );
    expect(page.text(page.popover)).toContain('Refresh AI use');

    (page.popover.querySelector('.usage-refresh') as HTMLButtonElement).focus();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    page.fixture.detectChanges();
    expect(page.pill.getAttribute('aria-expanded')).toBe('false');
    expect(page.popover.hidden).toBe(true);
    expect(document.activeElement).toBe(page.pill);
  });

  it('leaves focus that moved on to the page where it is when Escape closes it', () => {
    const page = render(today(1.8));
    const field = document.body.appendChild(document.createElement('input'));
    page.pill.click();
    page.fixture.detectChanges();
    field.focus();
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
    page.fixture.detectChanges();
    expect(page.pill.getAttribute('aria-expanded')).toBe('false');
    expect(document.activeElement).toBe(field);
  });

  it('closes on a second press and on a click outside', () => {
    const page = render(today(1.8));
    page.pill.click();
    page.fixture.detectChanges();
    page.pill.click();
    page.fixture.detectChanges();
    expect(page.pill.getAttribute('aria-expanded')).toBe('false');

    page.pill.click();
    page.fixture.detectChanges();
    document.body.click();
    page.fixture.detectChanges();
    expect(page.pill.getAttribute('aria-expanded')).toBe('false');
  });

  it('says when the allowance is used up', () => {
    const page = render(today(10));
    expect(page.text(page.pill)).toBe('AI allowance used up');
    expect(page.pill.classList.contains('used-up')).toBe(true);
  });

  it('says when it is still checking, or could not load', () => {
    const checking = render(null);
    expect(checking.text(checking.pill)).toBe('AI today · Checking…');
    const failed = render(null, true);
    expect(failed.text(failed.pill)).toBe('AI use unavailable');
    failed.pill.click();
    failed.fixture.detectChanges();
    expect(failed.text(failed.popover)).toContain('AI use could not be loaded.');
  });
});
