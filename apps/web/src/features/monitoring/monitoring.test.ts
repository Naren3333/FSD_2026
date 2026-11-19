// @vitest-environment jsdom
import { DOCUMENT, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, expect, it, vi } from 'vitest';
import { Gateway } from '../../core/gateway';
import { Session } from '../../core/session';
import { MonitoringComponent } from './monitoring';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

it('shows expenditure times in the budget timezone and names it', () => {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Gateway, useValue: { request: vi.fn(() => new Promise(() => undefined)) } },
      { provide: Session, useValue: { expired: signal(false) } },
      { provide: DOCUMENT, useValue: document },
    ],
  });
  TestBed.overrideComponent(MonitoringComponent, {
    set: { template: '', styleUrl: undefined, imports: [] },
  });
  const fixture = TestBed.createComponent(MonitoringComponent);
  try {
    const page = fixture.componentInstance;
    // 16:30 utc on the 17th is half past midnight on the 18th in singapore.
    const singapore = page.when('2026-11-17T16:30:05Z', 'Asia/Singapore');
    expect(singapore).toContain('18 Nov');
    expect(singapore).toContain('00:30:05');
    expect(singapore).not.toBe(page.when('2026-11-17T16:30:05Z', 'UTC'));
    expect(page.when('2026-11-17T16:30:05Z', 'UTC')).toMatch(/UTC/);
    expect(page.when(null)).toBe('');
  } finally {
    fixture.destroy();
  }
});

it.each([
  ['refreshStatus', 'status', '/status'],
  ['refreshSpend', 'spend', '/ai-spend'],
] as const)('%s replaces a background poll with a busy manual request', async (refresh, section, path) => {
  const requests: { signal: AbortSignal; complete: (data: object) => void }[] = [];
  const request = vi.fn(
    (_service: string, _path: string, options: { signal: AbortSignal }) =>
      new Promise<object>((complete) => requests.push({ signal: options.signal, complete })),
  );
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Gateway, useValue: { request } },
      { provide: Session, useValue: { expired: signal(false) } },
      { provide: DOCUMENT, useValue: document },
    ],
  });
  const visible = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  TestBed.overrideComponent(MonitoringComponent, {
    set: { template: '', styleUrl: undefined, imports: [] },
  });
  const fixture = TestBed.createComponent(MonitoringComponent);
  try {
    const page = fixture.componentInstance;
    page[refresh](false);
    expect(page[section].feedback.busy()).toBe(false);
    page[refresh]();
    expect(request).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenLastCalledWith('monitoring', path, expect.anything());
    expect(requests[0].signal.aborted).toBe(true);
    expect(page[section].feedback.busy()).toBe(true);
    page[refresh](false);
    expect(request).toHaveBeenCalledTimes(2);

    requests[0].complete({ stale: true });
    await Promise.resolve();
    expect(page[section].data()).toBeNull();
    expect(page[section].feedback.busy()).toBe(true);
    const current = { current: true };
    requests[1].complete(current);
    await Promise.resolve();
    expect(page[section].data()).toBe(current);
    expect(page[section].feedback.busy()).toBe(false);
  } finally {
    fixture.destroy();
    visible.mockRestore();
  }
});

it('trims pasted identifiers before filtering billed accounts', () => {
  const request = vi.fn(() => new Promise(() => undefined));
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: Gateway, useValue: { request } },
      { provide: Session, useValue: { expired: signal(false) } },
      { provide: DOCUMENT, useValue: document },
    ],
  });
  TestBed.overrideComponent(MonitoringComponent, {
    set: { template: '', styleUrl: undefined, imports: [] },
  });
  const fixture = TestBed.createComponent(MonitoringComponent);
  try {
    const page = fixture.componentInstance;
    page.filters.setValue({ user_id: '\tbilled-user \n', period: 'today' });
    page.applyFilters();
    expect(page.filters.getRawValue()).toEqual({ user_id: 'billed-user', period: 'today' });
    // monitoring reports the admin's own school only, so the page never asks for an organization.
    expect(request).toHaveBeenLastCalledWith(
      'monitoring',
      '/ai-spend/users?period=today&offset=0&user_id=billed-user',
      expect.anything(),
    );
  } finally {
    fixture.destroy();
  }
});
