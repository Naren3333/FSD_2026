import { NgTemplateOutlet } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ChatApi, ContentApi } from '../../api/clients';
import type { ChatMessage, Citation, StreamEvent, Thread } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { messageOf } from '../../shared/errors';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { dateTime, resetTime } from '../../shared/format';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import { Block, Inline, markdown, pages } from './markdown';

export const MAX_QUESTION = 1000;
const OUTCOME_NOTES: Record<string, string> = {
  no_results: 'No matching passage was found in this classroom’s indexed materials.',
  error: 'This answer failed before it finished. Ask again to retry.',
  interrupted: 'Stopped before the answer finished.',
};

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    NgTemplateOutlet,
  ],
  templateUrl: './chat.html',
})
export class ChatComponent {
  private readonly chat = inject(ChatApi);
  private readonly content = inject(ContentApi);
  private readonly usage = inject(AiUsage);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly max = MAX_QUESTION;
  readonly notes = OUTCOME_NOTES;
  readonly threads = new Section<Thread[]>();
  readonly active = signal<Thread | null>(null);
  readonly messages = new Section<ChatMessage[]>();
  readonly streaming = signal(false);
  readonly announcement = signal('');
  readonly tried = signal(false);
  readonly figures = signal<Record<string, string>>({});
  readonly figureErrors = signal<Record<string, string>>({});
  streamingId = '';
  /** Numbers each question's local placeholders, so they never share an id with an earlier question's. */
  private asked = 0;
  /** Each message's parsed answer, kept while the message object is unchanged; a streamed token makes a new one. */
  private readonly parsed = new WeakMap<ChatMessage, Block[]>();
  private controller: AbortController | null = null;
  readonly form = new FormGroup({
    question: new FormControl('', { nonNullable: true, validators: requiredText(MAX_QUESTION) }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.streaming());
    onClassroomChange(() => {
      this.controller?.abort();
      this.reset();
      this.loadThreads();
    });
  }
  /** Whether either list is loading; sending waits for both. */
  loading(): boolean {
    return this.threads.loading() || this.messages.loading();
  }
  private reset(): void {
    this.messages.clear();
    this.active.set(null);
    Object.values(this.figures()).forEach((url) => URL.revokeObjectURL(url));
    this.figures.set({});
    this.figureErrors.set({});
  }
  loadThreads(): void {
    const id = this.classroomId();
    this.threads.clear();
    if (!id) return;
    void this.threads.load(async () => (await this.chat.threads(id)).threads);
  }
  newThread(): void {
    this.reset();
    this.announcement.set('');
    setTimeout(() => document.getElementById('chat-question')?.focus());
  }
  open(thread: Thread): void {
    this.reset();
    this.active.set(thread);
    void this.messages.load(async () => (await this.chat.messages(thread.id)).messages);
  }

  shortcut(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      this.send();
    }
  }
  send(): void {
    if (this.streaming() || this.loading()) return;
    this.tried.set(true);
    if (!this.state.validate(this.form, 'chat-form')) return;
    const question = this.form.controls.question.value.trim(),
      classroom = this.classroomId();
    void this.state.run(async () => {
      let thread = this.active();
      if (!thread) {
        thread = await this.chat.createThread(classroom);
        this.active.set(thread);
        this.threads.set([thread, ...(this.threads.value() ?? [])]);
      }
      const turn = ++this.asked;
      const askedId = `asked-${turn}`;
      const answer: ChatMessage = {
        id: `streaming-${turn}`,
        role: 'assistant',
        content: '',
        outcome: null,
        citations: null,
        created_at: new Date().toISOString(),
      };
      this.messages.set([
        ...(this.messages.value() ?? []),
        {
          id: askedId,
          role: 'user',
          content: question,
          outcome: null,
          citations: null,
          created_at: answer.created_at,
        },
        answer,
      ]);
      this.streamingId = answer.id;
      this.streaming.set(true);
      this.announcement.set('Answering…');
      this.controller = new AbortController();
      let started = false,
        failure = '';
      const update = (change: Partial<ChatMessage>) =>
        this.messages.set(
          (this.messages.value() ?? []).map((m) => (m.id === answer.id ? { ...m, ...change } : m)),
        );
      try {
        await this.chat.ask(thread.id, question, this.controller.signal, (event: StreamEvent) => {
          if (!started) {
            started = true;
            this.form.reset();
            this.tried.set(false);
          }
          const current = this.messages.value()?.find((m) => m.id === answer.id);
          if (event.event === 'token')
            update({ content: (current?.content || '') + String(event.data['text'] ?? '') });
          else if (event.event === 'citations')
            update({ citations: (event.data['citations'] as Citation[]) || [] });
          else if (event.event === 'done')
            update({ outcome: event.data['outcome'] as ChatMessage['outcome'] });
          else if (event.event === 'error') {
            update({ outcome: 'error' });
            failure = String(event.data['message'] || 'The assistant could not answer. Retry later.');
            if (event.data['code'] === 'AI_SPEND_LIMIT' && typeof event.data['resets_at'] === 'string')
              failure += ` It resets at ${resetTime(event.data['resets_at'])}.`;
          }
        });
      } catch (error) {
        if (!started) {
          this.messages.set(
            (this.messages.value() ?? []).filter((m) => m.id !== answer.id && m.id !== askedId),
          );
          this.announcement.set('');
          throw error;
        }
        if (!this.controller?.signal.aborted) failure = messageOf(error, 'The answer stopped. Retry later.');
        update({ outcome: this.controller?.signal.aborted ? 'interrupted' : 'error' });
      } finally {
        this.streaming.set(false);
        this.streamingId = '';
        this.controller = null;
        // However the turn ended, it may have spent AI allowance.
        this.usage.refresh();
      }
      const done = this.messages.value()?.find((m) => m.id === answer.id);
      this.announcement.set(
        done?.outcome === 'answered'
          ? 'Answer complete.'
          : done?.outcome === 'interrupted'
            ? 'Answer stopped.'
            : done?.outcome === 'no_results'
              ? 'No matching material found.'
              : '',
      );
      await this.sync(thread);
      if (failure) throw new Error(failure);
    });
  }
  stop(): void {
    this.controller?.abort();
  }
  /** Replaces the local copy with what the chatbot saved, and moves the conversation to the top. */
  private async sync(thread: Thread): Promise<void> {
    try {
      const [{ messages }, { threads }] = await Promise.all([
        this.chat.messages(thread.id),
        this.chat.threads(thread.classroom_id),
      ]);
      if (this.active()?.id !== thread.id) return;
      this.messages.set(messages);
      this.threads.set(threads);
      this.active.set(threads.find((t) => t.id === thread.id) || thread);
    } catch {
      /* keep the streamed copy; Refresh conversations retries */
    }
  }

  blocks(message: ChatMessage): Block[] {
    let blocks = this.parsed.get(message);
    if (!blocks) {
      blocks = markdown(message.content, message.citations || []);
      this.parsed.set(message, blocks);
    }
    return blocks;
  }
  asRuns(runs: unknown): Inline[] {
    return runs as Inline[];
  }
  pageText(source: Citation): string {
    return pages(source);
  }
  sourceId(message: ChatMessage, n: number): string {
    return `source-${message.id}-${n}`;
  }
  jump(event: Event, message: ChatMessage, n: number): void {
    event.preventDefault();
    const item = document.getElementById(this.sourceId(message, n));
    if (!item) return;
    item.querySelector('details')?.setAttribute('open', '');
    item.focus();
  }
  reveal(event: Event, source: Citation): void {
    if (!(event.target as HTMLDetailsElement).open || !source.image || this.figures()[source.chunk_id])
      return;
    this.figureErrors.update((e) => ({ ...e, [source.chunk_id]: '' }));
    this.content
      .figure(source.document_id, source.image)
      .then((url) => this.figures.update((f) => ({ ...f, [source.chunk_id]: url })))
      .catch((error: unknown) =>
        this.figureErrors.update((e) => ({
          ...e,
          [source.chunk_id]: `The figure could not be loaded. ${messageOf(error, '')}`.trim(),
        })),
      );
  }
  /** The original PDF in a new tab at the source's first page; the tab opens on the click so it isn't blocked. */
  openPage(source: Citation): void {
    const tab = window.open('', '_blank');
    this.content
      .original(source.document_id)
      .then((url) => {
        if (tab) tab.location.href = `${url}#page=${source.page_start}`;
        else this.state.error.set('Allow pop-ups for this site to open the PDF.');
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      })
      .catch((error: unknown) => {
        tab?.close();
        this.state.error.set(`The PDF could not be opened. ${messageOf(error, '')}`.trim());
      });
  }
  when(iso: string): string {
    return dateTime(iso);
  }
}
