// @vitest-environment jsdom
import { Component, runInInjectionContext } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { Router, provideRouter } from '@angular/router';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { onSectionShown } from './section-shown';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

@Component({ standalone: true, template: '' })
class StubPage {}

/* a router with two sections, and a page on review that counts how often it is shown again. */
async function setup() {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        { path: 'review', component: StubPage },
        { path: 'home', component: StubPage },
      ]),
    ],
  });
  const router = TestBed.inject(Router);
  await router.navigateByUrl('/home');
  const shown = vi.fn();
  const fixture = TestBed.createComponent(StubPage);
  runInInjectionContext(fixture.componentRef.injector, () => onSectionShown('review', shown, 20_000));
  const go = (url: string) => router.navigateByUrl(url);
  return { shown, go, fixture };
}

describe('onSectionShown', () => {
  it('ignores the navigation that created the page', async () => {
    const page = await setup();
    vi.advanceTimersByTime(30_000);
    await page.go('/review');
    expect(page.shown).not.toHaveBeenCalled();
  });

  it('fires on a return after the minimum age, and not within it', async () => {
    const page = await setup();
    await page.go('/review');
    await page.go('/home');
    vi.advanceTimersByTime(10_000);
    await page.go('/review');
    expect(page.shown).not.toHaveBeenCalled();

    await page.go('/home');
    vi.advanceTimersByTime(20_000);
    await page.go('/review');
    expect(page.shown).toHaveBeenCalledTimes(1);

    await page.go('/home');
    vi.advanceTimersByTime(5_000);
    await page.go('/review');
    expect(page.shown).toHaveBeenCalledTimes(1);
  });

  it('ignores other sections and query-only changes on its own', async () => {
    const page = await setup();
    await page.go('/review');
    vi.advanceTimersByTime(30_000);
    await page.go('/review?grade=g1');
    await page.go('/review');
    expect(page.shown).not.toHaveBeenCalled();
    await page.go('/home');
    vi.advanceTimersByTime(30_000);
    await page.go('/home?x=1');
    expect(page.shown).not.toHaveBeenCalled();
  });

  it('stops after the page is destroyed', async () => {
    const page = await setup();
    await page.go('/review');
    await page.go('/home');
    page.fixture.destroy();
    vi.advanceTimersByTime(30_000);
    await page.go('/review');
    expect(page.shown).not.toHaveBeenCalled();
  });
});
