import { NgTemplateOutlet } from '@angular/common';
import {
  Component,
  ElementRef,
  ViewChild,
  afterRenderEffect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { ChatApi, ContentApi } from '../../api/clients';
import type { ChatMessage, Citation, StreamEvent, Thread } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { messageOf } from '../../shared/errors';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { dateTime, resetTime } from '../../shared/format';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Block, Inline, markdown, pages } from './markdown';
import { focusLater } from '../../shared/focus';

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
    PageHeaderComponent,
    RefreshButtonComponent,
    NoClassroomComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    NgTemplateOutlet,
    IconComponent,
  ],
  templateUrl: './chat.html',
})
export class ChatComponent {
  private readonly chat = inject(ChatApi);
  private readonly content = inject(ContentApi);
  private readonly usage = inject(AiUsage);
  private readonly context = inject(ClassroomContext);
  readonly classroomId = this.context.classroomId;
  /* whether the user's classrooms have loaded; until then the thread list says why there is no classroom. */
  readonly classroomsKnown = this.context.known;
  readonly state = new ActionState();
  readonly max = MAX_QUESTION;
  readonly notes = OUTCOME_NOTES;
  readonly threads = new Section<Thread[]>();
  readonly active = signal<Thread | null>(null);
  readonly messages = new Section<ChatMessage[]>();
  readonly streaming = signal(false);
  readonly announcement = signal('');
  readonly tried = signal(false);
  /* why an answer failed, by message id, shown under that answer instead of the generic note. */
  readonly failures = signal<Record<string, string>>({});
  readonly figures = signal<Record<string, string>>({});
  readonly figureErrors = signal<Record<string, string>>({});
  streamingId = '';
  /* numbers each question's local placeholders, so they never share an id with an earlier question's. */
  private asked = 0;
  /* each message's parsed answer, kept while the message object is unchanged; a streamed token makes a new one. */
  private readonly parsed = new WeakMap<ChatMessage, Block[]>();
  private controller: AbortController | null = null;
  readonly form = new FormGroup({
    question: new FormControl('', { nonNullable: true, validators: requiredText(MAX_QUESTION) }),
  });
  /* the message list's scroll box, which follows a streaming answer while the user is at its end. */
  @ViewChild('scroller') private scroller?: ElementRef<HTMLElement>;
  /* the user scrolled up and newer messages are below them. */
  readonly behind = signal(false);
  private pinned = true;
  /* the thread whose end was last scrolled to, so a newly opened one starts at its end. */
  private shownThread: string | null | undefined = undefined;
  private wasStreaming = false;
  private readonly follow = afterRenderEffect(() => {
    const list = this.messages.value();
    const loading = this.messages.loading();
    const thread = this.active()?.id ?? null;
    const streaming = this.streaming();
    untracked(() => {
      const box = this.scroller?.nativeElement;
      if (!box || loading) return;
      // a newly opened thread, or a question just sent, shows its end.
      const started = streaming && !this.wasStreaming;
      this.wasStreaming = streaming;
      if ((list && thread !== this.shownThread) || started) {
        this.shownThread = thread;
        this.pinned = true;
      }
      // an emptied list, such as a new conversation, has nothing to jump to.
      if (this.pinned) this.scrollToEnd(box);
      else this.behind.set(!!list?.length);
    });
  });

  /* whether the user is at the end of the messages, read as they scroll. */
  onScroll(): void {
    const box = this.scroller?.nativeElement;
    if (!box) return;
    this.pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 24;
    if (this.pinned) this.behind.set(false);
  }
  jumpToLatest(): void {
    const box = this.scroller?.nativeElement;
    this.pinned = true;
    if (!box) return;
    this.scrollToEnd(box);
    // the button goes away, so keyboard focus moves to the messages instead of the page.
    box.focus({ preventScroll: true });
  }
  private scrollToEnd(box: HTMLElement): void {
    box.scrollTop = box.scrollHeight;
    this.behind.set(false);
  }

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.streaming());
    onClassroomChange(() => {
      this.controller?.abort();
      this.reset();
      this.loadThreads();
    });
  }
  /* whether either list is loading; sending waits for both. */
  loading(): boolean {
    return this.threads.loading() || this.messages.loading();
  }
  private reset(): void {
    this.messages.clear();
    this.active.set(null);
    Object.values(this.figures()).forEach((url) => URL.revokeObjectURL(url));
    this.figures.set({});
    this.figureErrors.set({});
    this.failures.set({});
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
    focusLater('chat-question');
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
    // the question moves into the conversation at once; it comes back only if it was never asked.
    this.form.reset();
    this.tried.set(false);
    // a restored question is unsaved work again, so the shell keeps the classroom until it is sent or
    // cleared.
    const restore = () => {
      if (this.form.controls.question.value) return;
      this.form.controls.question.setValue(question);
      this.form.markAsDirty();
    };
    void this.state.run(async () => {
      let thread = this.active();
      if (!thread) {
        thread = await this.chat.createThread(classroom).catch((error: unknown) => {
          restore();
          throw error;
        });
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
          started = true;
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
          restore();
          throw error;
        }
        if (!this.controller?.signal.aborted) failure = messageOf(error, 'The answer stopped. Retry later.');
        update({ outcome: this.controller?.signal.aborted ? 'interrupted' : 'error' });
      } finally {
        this.streaming.set(false);
        this.streamingId = '';
        this.controller = null;
        // however the turn ended, it may have spent ai allowance.
        this.usage.refresh();
      }
      if (failure) this.failures.update((all) => ({ ...all, [answer.id]: failure }));
      const done = this.messages.value()?.find((m) => m.id === answer.id);
      this.announcement.set(
        done?.outcome === 'answered'
          ? 'Answer complete.'
          : done?.outcome === 'interrupted'
            ? 'Answer stopped.'
            : done?.outcome === 'no_results'
              ? 'No matching material found.'
              : failure
                ? 'Answer failed.'
                : '',
      );
      await this.sync(thread);
      if (failure) this.moveFailure(answer.id);
    });
  }
  /* once the saved conversation replaces the streamed answer, its failure moves to the saved answer. */
  private moveFailure(localId: string): void {
    const list = this.messages.value() ?? [];
    if (list.some((m) => m.id === localId)) return;
    const saved = [...list].reverse().find((m) => m.role === 'assistant');
    if (!saved) return;
    this.failures.update(({ [localId]: reason, ...rest }) => ({ ...rest, [saved.id]: reason }));
  }
  stop(): void {
    this.controller?.abort();
  }
  /* replaces the local copy with what the chatbot saved, and moves the conversation to the top. */
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
      /* retain streamed text until the conversation can be refreshed. */
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
    const open = (event.target as HTMLDetailsElement).open;
    if (!open || source.removed || !source.image || this.figures()[source.chunk_id]) return;
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
  /* open the original file; create the tab synchronously to avoid popup blocking. */
  openPage(source: Citation): void {
    const tab = window.open('', '_blank');
    this.content
      .original(source.document_id)
      .then((url) => {
        if (tab)
          tab.location.href =
            source.source === 'pdf' || source.source === 'vision' ? `${url}#page=${source.page_start}` : url;
        else this.state.error.set('Allow pop-ups for this site to open the document.');
        setTimeout(() => URL.revokeObjectURL(url), 60000);
      })
      .catch((error: unknown) => {
        tab?.close();
        this.state.error.set(`The document could not be opened. ${messageOf(error, '')}`.trim());
      });
  }
  when(iso: string): string {
    return dateTime(iso);
  }
}
