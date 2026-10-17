// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ChatApi, ContentApi } from '../../api/clients';
import type { StreamEvent, Thread } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext } from '../../core/classroom-context';
import { ChatComponent } from './chat';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const thread: Thread = {
  id: 't1',
  classroom_id: 'c1',
  role: 'student',
  title: null,
  created_at: '2026-10-17T00:00:00Z',
  last_message_at: '2026-10-17T00:00:00Z',
};

describe('chat placeholders', () => {
  it('gives each question its own local ids, so a failed sync never merges two answers', async () => {
    const chat = {
      threads: vi.fn(() => Promise.resolve({ threads: [] as Thread[] })),
      createThread: vi.fn(() => Promise.resolve(thread)),
      // Re-reading the saved conversation fails, so the local copies stay.
      messages: vi.fn(() => Promise.reject(new Error('offline'))),
      ask: vi.fn((_t: string, content: string, _s: AbortSignal, onEvent: (e: StreamEvent) => void) => {
        onEvent({ event: 'token', data: { text: `About ${content}` } });
        onEvent({ event: 'done', data: { outcome: 'answered' } });
        return Promise.resolve();
      }),
    };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ChatApi, useValue: chat },
        { provide: ContentApi, useValue: {} },
        { provide: AiUsage, useValue: { refresh: () => undefined } },
        { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
      ],
    });
    TestBed.overrideComponent(ChatComponent, { set: { template: '', imports: [] } });
    const page = TestBed.createComponent(ChatComponent).componentInstance;
    await new Promise((resolve) => setTimeout(resolve));
    for (const question of ['fractions', 'decimals']) {
      page.form.controls.question.setValue(question);
      page.send();
      await vi.waitFor(() => expect(page.state.busy()).toBe(false));
    }
    const messages = page.messages.value() ?? [];
    expect(new Set(messages.map((m) => m.id)).size).toBe(4);
    expect(messages.filter((m) => m.role === 'assistant').map((m) => m.content)).toEqual([
      'About fractions',
      'About decimals',
    ]);
    // A message's parsed Markdown is kept until the message changes.
    expect(page.blocks(messages[1])).toBe(page.blocks(messages[1]));
  });
});
