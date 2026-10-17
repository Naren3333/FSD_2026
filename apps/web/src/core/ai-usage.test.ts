// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ChatApi } from '../api/clients';
import type { Usage } from '../api/models';
import { AiUsage } from './ai-usage';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const spent = (spent_usd: number): Usage => ({ spent_usd, limit_usd: 1, resets_at: '2026-10-18T00:00:00Z' });

function setup(usage: () => Promise<Usage>) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({ providers: [{ provide: ChatApi, useValue: { usage: vi.fn(usage) } }] });
  return TestBed.inject(AiUsage);
}

describe('AiUsage', () => {
  it('keeps the newest read when an older one answers last', async () => {
    const answers: ((u: Usage) => void)[] = [];
    const meter = setup(() => new Promise((yes) => answers.push(yes)));
    meter.refresh();
    meter.refresh();
    answers[1](spent(0.4));
    answers[0](spent(0.1));
    await vi.waitFor(() => expect(meter.loading()).toBe(false));
    expect(meter.usage()).toEqual(spent(0.4));
  });

  it('reports a failed read, and recovers on the next', async () => {
    let fail = true;
    const meter = setup(() => (fail ? Promise.reject(new Error('offline')) : Promise.resolve(spent(0.2))));
    meter.refresh();
    await vi.waitFor(() => expect(meter.failed()).toBe(true));
    expect(meter.usage()).toBeNull();
    fail = false;
    meter.refresh();
    await vi.waitFor(() => expect(meter.usage()).toEqual(spent(0.2)));
    expect(meter.failed()).toBe(false);
  });
});
