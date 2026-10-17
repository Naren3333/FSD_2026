// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ChatApi, ContentApi } from '../../api/clients';
import type { StreamEvent, Thread } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext } from '../../core/classroom-context';
import { resetTime } from '../../shared/format';
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

/** The chat page's logic (no template) whose answers stream as `ask` says, with a spy on the AI use meter. */
async function setup(ask: (onEvent: (e: StreamEvent) => void) => Promise<void>) {
  const usage = { refresh: vi.fn() };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: ChatApi,
        useValue: {
          threads: () => Promise.resolve({ threads: [thread] }),
          createThread: () => Promise.resolve(thread),
          messages: () => Promise.resolve({ messages: [] }),
          ask: (_t: string, _c: string, _s: AbortSignal, onEvent: (e: StreamEvent) => void) => ask(onEvent),
        },
      },
      { provide: ContentApi, useValue: {} },
      { provide: AiUsage, useValue: usage },
      { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
    ],
  });
  TestBed.overrideComponent(ChatComponent, { set: { template: '', imports: [] } });
  const page = TestBed.createComponent(ChatComponent).componentInstance;
  await vi.waitFor(() => expect(page.threads.value()).toEqual([thread]));
  const send = async (question: string) => {
    page.form.controls.question.setValue(question);
    page.send();
    await vi.waitFor(() => expect(page.state.busy()).toBe(false));
  };
  return { page, usage, send };
}

describe('a chat answer that fails', () => {
  it('says when a used-up daily AI allowance resets, and refreshes the meter', async () => {
    const resets = '2026-10-18T00:00:00+08:00';
    const { page, usage, send } = await setup((onEvent) => {
      onEvent({
        event: 'error',
        data: { code: 'AI_SPEND_LIMIT', message: "You've used today's AI allowance.", resets_at: resets },
      });
      return Promise.resolve();
    });
    usage.refresh.mockClear();
    await send('What is a fraction?');
    expect(page.state.error()).toBe(`You've used today's AI allowance. It resets at ${resetTime(resets)}.`);
    expect(usage.refresh).toHaveBeenCalled();
  });

  it('adds no reset time to other failures', async () => {
    const { page, send } = await setup((onEvent) => {
      onEvent({
        event: 'error',
        data: { code: 'AI_UNAVAILABLE', message: 'The assistant is not available.' },
      });
      return Promise.resolve();
    });
    await send('What is a fraction?');
    expect(page.state.error()).toBe('The assistant is not available.');
  });

  it('keeps the question for retry and refreshes the meter when the chatbot refuses it', async () => {
    const { page, usage, send } = await setup(() => Promise.reject(new Error('The chatbot is unavailable.')));
    usage.refresh.mockClear();
    await send('What is a fraction?');
    expect(page.state.error()).toBe('The chatbot is unavailable.');
    expect(page.form.controls.question.value).toBe('What is a fraction?');
    expect(page.messages.value()).toEqual([]);
    expect(usage.refresh).toHaveBeenCalled();
  });
});
