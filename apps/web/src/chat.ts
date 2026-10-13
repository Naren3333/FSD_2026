import { Component, EventEmitter, Input, OnChanges, Output, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, ChatMessage, Citation, StreamEvent, Thread } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';
import { NgTemplateOutlet } from '@angular/common';
import { Inline, markdown } from './markdown';
import { pages } from './sse';
import { resetTime } from './usage';

export const MAX_QUESTION = 1000;
const OUTCOME_NOTES: Record<string, string> = {
  no_results: 'No matching passage was found in this classroom’s indexed materials.',
  error: 'This answer failed before it finished. Ask again to retry.',
  interrupted: 'Stopped before the answer finished.',
};

@Component({
  selector: 'app-chat',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent, NgTemplateOutlet],
  templateUrl: './chat.html',
})
export class ChatComponent implements OnChanges {
  readonly api = inject(Api);
  readonly state = new ActionState();
  readonly max = MAX_QUESTION;
  readonly notes = OUTCOME_NOTES;
  @Input() classroomId = '';
  /** Fires when a turn ends, however it ended, so the AI use meter can catch up. */
  @Output() readonly turnEnded = new EventEmitter<void>();
  readonly threads = signal<Thread[]>([]);
  readonly active = signal<Thread | null>(null);
  readonly messages = signal<ChatMessage[]>([]);
  readonly threadsLoading = signal(false);
  readonly messagesLoading = signal(false);
  readonly streaming = signal(false);
  readonly announcement = signal('');
  readonly tried = signal(false);
  readonly figures = signal<Record<string, string>>({});
  readonly figureErrors = signal<Record<string, string>>({});
  streamingId = '';
  private controller: AbortController | null = null;
  private threadRevision = 0;
  private messageRevision = 0;
  readonly form = new FormGroup({
    question: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(MAX_QUESTION), Validators.pattern(/\S/)],
    }),
  });

  ngOnChanges(): void {
    this.controller?.abort();
    this.threads.set([]);
    this.reset();
    this.loadThreads();
  }
  private reset(): void {
    this.messageRevision++;
    this.messagesLoading.set(false);
    this.active.set(null);
    this.messages.set([]);
    Object.values(this.figures()).forEach((url) => URL.revokeObjectURL(url));
    this.figures.set({});
    this.figureErrors.set({});
  }
  loadThreads(): void {
    const id = this.classroomId,
      revision = ++this.threadRevision;
    if (!id) return;
    this.threadsLoading.set(true);
    void this.state.run(async () => {
      try {
        const { threads } = await this.api.threads(id);
        if (revision === this.threadRevision) this.threads.set(threads);
      } finally {
        if (revision === this.threadRevision) this.threadsLoading.set(false);
      }
    });
  }
  newThread(): void {
    this.reset();
    this.announcement.set('');
    setTimeout(() => document.getElementById('chat-question')?.focus());
  }
  open(thread: Thread): void {
    this.reset();
    this.active.set(thread);
    const revision = ++this.messageRevision;
    this.messagesLoading.set(true);
    void this.state.run(async () => {
      try {
        const { messages } = await this.api.messages(thread.id);
        if (revision === this.messageRevision) this.messages.set(messages);
      } finally {
        if (revision === this.messageRevision) this.messagesLoading.set(false);
      }
    });
  }

  shortcut(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      this.send();
    }
  }
  send(): void {
    if (this.streaming()) return;
    this.tried.set(true);
    if (!this.state.validate(this.form)) return;
    const question = this.form.controls.question.value.trim(),
      classroom = this.classroomId;
    void this.state.run(async () => {
      let thread = this.active();
      if (!thread) {
        thread = await this.api.createThread(classroom);
        this.active.set(thread);
        this.threads.update((list) => [thread as Thread, ...list]);
      }
      const answer: ChatMessage = {
        id: 'streaming',
        role: 'assistant',
        content: '',
        outcome: null,
        citations: null,
        created_at: new Date().toISOString(),
      };
      this.messages.update((list) => [
        ...list,
        {
          id: 'asked',
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
        this.messages.update((list) => list.map((m) => (m.id === answer.id ? { ...m, ...change } : m)));
      try {
        await this.api.stream(
          'chatbot',
          `/threads/${thread.id}/messages`,
          { content: question },
          this.controller.signal,
          (event: StreamEvent) => {
            if (!started) {
              started = true;
              this.form.reset();
              this.tried.set(false);
            }
            const current = this.messages().find((m) => m.id === answer.id);
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
          },
        );
      } catch (error) {
        if (!started) {
          this.messages.update((list) => list.filter((m) => m.id !== answer.id && m.id !== 'asked'));
          this.announcement.set('');
          throw error;
        }
        if (!this.controller?.signal.aborted)
          failure = error instanceof Error ? error.message : 'The answer stopped. Retry later.';
        update({ outcome: this.controller?.signal.aborted ? 'interrupted' : 'error' });
      } finally {
        this.streaming.set(false);
        this.streamingId = '';
        this.controller = null;
        this.turnEnded.emit();
      }
      const done = this.messages().find((m) => m.id === answer.id);
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
        this.api.messages(thread.id),
        this.api.threads(thread.classroom_id),
      ]);
      if (this.active()?.id !== thread.id) return;
      this.messages.set(messages);
      this.threads.set(threads);
      this.active.set(threads.find((t) => t.id === thread.id) || thread);
    } catch {
      /* keep the streamed copy; Refresh conversations retries */
    }
  }

  blocks(message: ChatMessage) {
    return markdown(message.content, message.citations || []);
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
    this.api
      .objectUrl(
        'content',
        `/documents/${encodeURIComponent(source.document_id)}/figures/${encodeURIComponent(source.image)}`,
      )
      .then((url) => this.figures.update((f) => ({ ...f, [source.chunk_id]: url })))
      .catch((error: unknown) =>
        this.figureErrors.update((e) => ({
          ...e,
          [source.chunk_id]:
            error instanceof Error
              ? `The figure could not be loaded. ${error.message}`
              : 'The figure could not be loaded.',
        })),
      );
  }
  /** The original PDF in a new tab at the source's first page; the tab opens on the click so it isn't blocked. */
  openPage(source: Citation): void {
    const tab = window.open('', '_blank');
    this.api
      .objectUrl('content', `/documents/${encodeURIComponent(source.document_id)}/original`)
      .then((url) => {
        if (tab) tab.location.href = `${url}#page=${source.page_start}`;
        else this.state.error.set('Allow pop-ups for this site to open the PDF.');
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      })
      .catch((error: unknown) => {
        tab?.close();
        this.state.error.set(
          error instanceof Error
            ? `The PDF could not be opened. ${error.message}`
            : 'The PDF could not be opened.',
        );
      });
  }
  when(iso: string): string {
    return new Date(iso).toLocaleString('en-SG', { dateStyle: 'medium', timeStyle: 'short' });
  }
}
