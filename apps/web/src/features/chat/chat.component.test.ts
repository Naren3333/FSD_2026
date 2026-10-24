// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ChatApi, ContentApi } from '../../api/clients';
import type { ChatMessage, Citation, StreamEvent, Thread } from '../../api/models';
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
      // re-reading the saved conversation fails, so the local copies stay.
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
    // a message's parsed markdown is kept until the message changes.
    expect(page.blocks(messages[1])).toBe(page.blocks(messages[1]));
  });
});

/* the chat page's logic (no template) whose answers stream as `ask` says, with a spy on the ai use meter. */
async function setup(ask: (onEvent: (e: StreamEvent) => void) => Promise<void>, saved: ChatMessage[] = []) {
  const usage = { refresh: vi.fn() };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: ChatApi,
        useValue: {
          threads: () => Promise.resolve({ threads: [thread] }),
          createThread: () => Promise.resolve(thread),
          messages: () => Promise.resolve({ messages: saved }),
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

describe('sending a question', () => {
  it('clears the question box as soon as it is sent, before the answer starts', async () => {
    let finish = () => undefined as void;
    const { page } = await setup(
      (onEvent) =>
        new Promise<void>((resolve) => {
          finish = () => {
            onEvent({ event: 'done', data: { outcome: 'answered' } });
            resolve();
          };
        }),
    );
    page.form.controls.question.setValue('What is a fraction?');
    page.send();
    expect(page.form.controls.question.value).toBe('');
    expect(page.form.dirty).toBe(false);
    await vi.waitFor(() => expect(page.streaming()).toBe(true));
    expect(page.messages.value()?.[0].content).toBe('What is a fraction?');
    finish();
    await vi.waitFor(() => expect(page.state.busy()).toBe(false));
    expect(page.form.controls.question.value).toBe('');
  });
});

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
    expect(Object.values(page.failures())).toEqual([
      `You've used today's AI allowance. It resets at ${resetTime(resets)}.`,
    ]);
    // the reason shows under the answer only, not again in the page status.
    expect(page.state.error()).toBe('');
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
    expect(Object.values(page.failures())).toEqual(['The assistant is not available.']);
    expect(page.state.error()).toBe('');
    // announced briefly, as the other outcomes are; the reason is read under the answer.
    expect(page.announcement()).toBe('Answer failed.');
  });

  it('keeps the reason with the saved answer once the conversation is re-read', async () => {
    const at = '2026-10-24T00:00:00Z';
    const saved: ChatMessage[] = [
      {
        id: 'm1',
        role: 'user',
        content: 'What is a fraction?',
        outcome: null,
        citations: null,
        created_at: at,
      },
      { id: 'm2', role: 'assistant', content: '', outcome: 'error', citations: null, created_at: at },
    ];
    const { page, send } = await setup((onEvent) => {
      onEvent({
        event: 'error',
        data: { code: 'AI_UNAVAILABLE', message: 'The assistant is not available.' },
      });
      return Promise.resolve();
    }, saved);
    await send('What is a fraction?');
    expect(page.failures()).toEqual({ m2: 'The assistant is not available.' });
    page.newThread();
    expect(page.failures()).toEqual({});
  });

  it('keeps the question for retry and refreshes the meter when the chatbot refuses it', async () => {
    const { page, usage, send } = await setup(() => Promise.reject(new Error('The chatbot is unavailable.')));
    usage.refresh.mockClear();
    await send('What is a fraction?');
    expect(page.state.error()).toBe('The chatbot is unavailable.');
    expect(page.form.controls.question.value).toBe('What is a fraction?');
    expect(page.form.dirty).toBe(true);
    expect(page.messages.value()).toEqual([]);
    expect(usage.refresh).toHaveBeenCalled();
  });
});

describe('a source whose material was deleted', () => {
  it('loads no figure for it', async () => {
    const content = { figure: vi.fn(() => Promise.resolve('blob:figure')) };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: ChatApi,
          useValue: { threads: () => Promise.resolve({ threads: [] as Thread[] }) },
        },
        { provide: ContentApi, useValue: content },
        { provide: AiUsage, useValue: { refresh: () => undefined } },
        { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
      ],
    });
    TestBed.overrideComponent(ChatComponent, { set: { template: '', imports: [] } });
    const page = TestBed.createComponent(ChatComponent).componentInstance;
    const source: Citation = {
      chunk_id: 'k1',
      document_id: 'd1',
      title: 'Notes',
      page_start: 1,
      page_end: 1,
      kind: 'figure',
      source: 'pdf',
      content: '',
      image: null,
      removed: true,
    };
    const opened = { target: { open: true } } as unknown as Event;
    page.reveal(opened, source);
    page.reveal(opened, { ...source, image: 'figure.png' });
    expect(content.figure).not.toHaveBeenCalled();
    page.reveal(opened, { ...source, image: 'figure.png', removed: false });
    expect(content.figure).toHaveBeenCalledWith('d1', 'figure.png');
  });
});

describe('following the conversation', () => {
  const said = (id: string, content: string): ChatMessage => ({
    id,
    role: 'assistant',
    content,
    outcome: 'answered',
    citations: null,
    created_at: '2026-10-17T00:00:00Z',
  });

  /* the chat page with only its message scroll box, whose layout jsdom lacks, so its sizes are stubbed. */
  async function scrolling() {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: ChatApi,
          useValue: {
            threads: () => Promise.resolve({ threads: [thread] }),
            messages: () => Promise.resolve({ messages: [said('m1', 'First')] }),
          },
        },
        { provide: ContentApi, useValue: {} },
        { provide: AiUsage, useValue: { refresh: () => undefined } },
        { provide: ClassroomContext, useValue: { classroomId: signal('c1') } },
      ],
    });
    TestBed.overrideComponent(ChatComponent, {
      set: {
        template: `<div #scroller tabindex="0" (scroll)="onScroll()"></div>
          @if (behind()) {<button type="button" (click)="jumpToLatest()">Jump to latest</button>}`,
        imports: [],
      },
    });
    const fixture = TestBed.createComponent(ChatComponent);
    fixture.detectChanges();
    const page = fixture.componentInstance;
    const root = fixture.nativeElement as HTMLElement;
    const box = root.querySelector('div') as HTMLElement;
    const size = { height: 1000, top: 0 };
    Object.defineProperty(box, 'scrollHeight', { get: () => size.height });
    Object.defineProperty(box, 'clientHeight', { get: () => 400 });
    Object.defineProperty(box, 'scrollTop', {
      get: () => size.top,
      set: (value: number) => (size.top = Math.min(value, size.height - 400)),
    });
    const render = async () => {
      await new Promise((resolve) => setTimeout(resolve));
      TestBed.tick();
    };
    page.open(thread);
    await render();
    /* a longer conversation, as a streamed token makes it. */
    const grow = async (content: string) => {
      size.height += 200;
      page.messages.set([said('m1', content)]);
      await render();
    };
    const jump = () =>
      [...root.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Jump to latest');
    return { page, box, size, grow, jump, render };
  }

  it('opens a thread at its end and stays there as the answer grows', async () => {
    const { size, grow, jump } = await scrolling();
    expect(size.top).toBe(600);
    await grow('First, longer');
    expect(size.top).toBe(800);
    expect(jump()).toBeUndefined();
  });

  it('leaves a user who scrolled up where they are, and offers Jump to latest', async () => {
    const { box, size, grow, jump, render } = await scrolling();
    size.top = 100;
    box.dispatchEvent(new Event('scroll'));
    await grow('First, longer');
    expect(size.top).toBe(100);
    const button = jump();
    expect(button).toBeDefined();
    button?.click();
    await render();
    expect(size.top).toBe(800);
    expect(jump()).toBeUndefined();
    expect(document.activeElement).toBe(box);
  });

  it('hides Jump to latest once a new conversation empties the list', async () => {
    const { page, box, size, grow, jump, render } = await scrolling();
    size.top = 100;
    box.dispatchEvent(new Event('scroll'));
    await grow('First, longer');
    expect(jump()).toBeDefined();
    page.newThread();
    await render();
    expect(jump()).toBeUndefined();
  });
});
