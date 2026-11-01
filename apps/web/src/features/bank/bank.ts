import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, type ValidatorFn } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { AssessmentApi } from '../../api/clients';
import type { BankQuestion, QuestionType, Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { messageOf } from '../../shared/errors';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { focusLater } from '../../shared/focus';
import { IconComponent } from '../../shared/icon';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagedSection } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section, rereadAfterDelete } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { BANK_TYPES } from '../assessments/bank-picker';
import {
  MAX_CRITERIA,
  criterionForm,
  forBank,
  fromBank,
  marksText,
  parseLevels,
  questionForm,
  rubricTotal,
  type QuestionForm,
} from '../assessments/editor';

/* the teacher's question bank (adr 0017): the list, filtered and paged, and one question's form, chosen by the address (`?edit=<id>`, or `?edit=new`). */
@Component({
  selector: 'app-question-bank',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    RouterLink,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    IconComponent,
    PagerComponent,
    SectionStateComponent,
    StatusComponent,
    ConfirmDialogComponent,
  ],
  templateUrl: './bank.html',
})
export class QuestionBankComponent {
  private readonly api = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly query = toSignal(this.route.queryParamMap);
  readonly state = new ActionState();
  /* deleting a bank question, in its confirmation dialog. */
  readonly deleteState = new ActionState();
  /* the bank question waiting for its deletion to be confirmed. */
  readonly deleting = signal<BankQuestion | null>(null);
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  readonly questions = new PagedSection<BankQuestion>();
  /* reading the curriculum, for the topic filter and the form's topics. */
  readonly topicsState = new Section<Topic[]>();
  /* the bank question the address names to edit, read when the list does not hold it. */
  readonly opened = new Section<BankQuestion>();
  readonly topics = this.curriculum.topics;
  readonly types = BANK_TYPES;
  readonly maxCriteria = MAX_CRITERIA;
  readonly marksText = marksText;
  readonly parseLevels = parseLevels;
  readonly filters = new FormGroup({
    text: new FormControl('', { nonNullable: true }),
    topic: new FormControl('', { nonNullable: true }),
    type: new FormControl<'' | QuestionType>('', { nonNullable: true }),
  });
  /* the filter the list on show was read with, so the empty text and paging use it, not unsent changes. */
  private readonly filter = signal<{ text: string; topicId: string; type: '' | QuestionType }>({
    text: '',
    topicId: '',
    type: '',
  });
  readonly filtered = computed(() => {
    const f = this.filter();
    return !!(f.text.trim() || f.topicId || f.type);
  });
  /* the question being edited: one form at a time, replaced when another is opened. */
  readonly form = signal<QuestionForm>(this.blankForm());
  /* what the form holds: '' for nothing, 'new' for a new question, or the bank question's id. */
  readonly editKey = signal('');
  readonly view = computed(() => (this.query()?.get('edit') ? 'edit' : 'list'));

  constructor() {
    // the bank belongs to the teacher, not a classroom, so changing classrooms keeps the form.
    inject(UnsavedChanges).track(() => this.form().dirty, { locksClassroom: false });
    this.route.queryParamMap
      .pipe(takeUntilDestroyed())
      .subscribe((params) => this.follow(params.get('edit') ?? ''));
    onSectionShown('question-bank', () => rereadQuietly(this.questions, () => this.readList()));
    this.load();
  }

  private readList(): Promise<BankQuestion[]> {
    const f = this.filter();
    const filter = { topicId: f.topicId, type: f.type || undefined, text: f.text };
    return this.questions.readPage((offset, limit) => this.api.bankPage(filter, offset, limit));
  }

  load(): void {
    this.readPage();
    // read fresh, so a topic a colleague just added is there to choose.
    void this.topicsState.load(() => this.curriculum.refresh());
  }

  /* reads the bank from its first page with the filters as they now are. */
  search(): void {
    const { text, topic, type } = this.filters.getRawValue();
    this.filter.set({ text, topicId: topic, type });
    this.questions.page.set(0);
    this.readPage();
  }

  /* reads the page on show. */
  readPage(): void {
    void this.questions.load(() => this.readList());
  }

  clearFilters(): void {
    this.filters.reset();
    this.search();
  }

  /* re-reads the list after a change; a failure keeps what is on show and says so. */
  private async reread(): Promise<void> {
    await this.questions.reload(() => this.readList());
  }

  topicName(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  /* a question on an archived topic stays in the bank but cannot go into an assessment. */
  archived(topicId: string): boolean {
    return this.curriculum.archived().some((t) => t.id === topicId);
  }

  typeName(type: QuestionType): string {
    return BANK_TYPES.find((t) => t.value === type)?.label ?? type;
  }

  // --- the form, chosen by the address
  // --------------------------------------------------------------------------------

  /* an archived topic is out of every picker for new questions, so a question that still has one must change it before it saves. */
  private readonly activeTopic: ValidatorFn = (control) =>
    this.archived(control.value as string) ? { archived: true } : null;

  private blankForm(question?: BankQuestion): QuestionForm {
    const form = questionForm(question ? fromBank(question) : undefined);
    form.controls.topic_id.addValidators(this.activeTopic);
    return form;
  }

  /* follows the address: the form for the question it names, or the list. */
  private follow(edit: string): void {
    if (edit === this.editKey()) return;
    if (!edit) {
      // an unsaved form stays, and the list offers to go back to it.
      if (!this.form().dirty) this.editKey.set('');
      return;
    }
    if (this.form().dirty) {
      this.state.error.set('Save or discard the question you are editing first.');
      void this.showForm(this.editKey(), true);
      return;
    }
    this.state.error.set('');
    this.state.message.set('');
    this.editKey.set(edit);
    if (edit === 'new') {
      this.opened.clear();
      this.form.set(this.blankForm());
      focusLater('bank-question-type');
      return;
    }
    const listed = this.questions.value()?.find((q) => q.id === edit);
    void this.opened.load(async (current) => {
      const question = listed ?? (await this.api.bankQuestion(edit).catch(refusedOpen));
      if (current() && this.editKey() === edit) {
        this.form.set(this.blankForm(question));
        focusLater('bank-question-prompt');
      }
      return question;
    });
  }

  private showForm(key: string, replaceUrl = false): Promise<boolean> {
    return this.router.navigate([], { relativeTo: this.route, queryParams: { edit: key }, replaceUrl });
  }

  /* back to the list (`replaceUrl` after a save, so back does not reopen the form), focusing `focus`. */
  private showList(replaceUrl: boolean, focus: string): void {
    void this.router
      .navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl })
      .then(() => focusLater(focus));
  }

  newQuestion(): void {
    void this.showForm('new');
  }

  edit(question: BankQuestion): void {
    void this.showForm(question.id);
  }

  /* discards the form and returns to the list. */
  cancel(): void {
    const key = this.editKey();
    this.form.set(this.blankForm());
    this.editKey.set('');
    this.opened.clear();
    this.state.error.set('');
    this.showList(false, key && key !== 'new' ? `edit-${key}` : 'new-question');
  }

  /* the start of the unsaved question's prompt, for the list's notice. */
  unsavedName(): string {
    const prompt = this.form().controls.prompt.value.trim().split('\n')[0] ?? '';
    if (!prompt) return this.editKey() === 'new' ? 'a new question' : 'a bank question';
    return `“${prompt.length > 60 ? prompt.slice(0, 57).trimEnd() + '…' : prompt}”`;
  }

  save(): void {
    const form = this.form();
    const key = this.editKey();
    // a topic archived since the form opened is caught here, not by the server.
    form.controls.topic_id.updateValueAndValidity();
    if (!this.state.validate(form, 'bank-form')) return;
    const question = forBank(form);
    void this.state
      .run(
        async () => {
          try {
            if (key === 'new') await this.api.addToBank(question);
            else await this.api.updateBankQuestion(key, question);
          } catch (error) {
            throw this.refusedSave(error, form);
          }
          form.markAsPristine();
          this.form.set(this.blankForm());
          this.editKey.set('');
          this.opened.clear();
          this.showList(true, 'bank-title');
        },
        key === 'new' ? 'Question added to your bank.' : 'Bank question saved.',
      )
      .then(async () => {
        if (this.state.error()) return;
        // the bank lists the newest change first, so the saved question is at the top of page one.
        this.questions.page.set(0);
        await this.reread().catch((error: unknown) =>
          this.state.error.set(
            `The question bank could not be refreshed. ${messageOf(error, 'Choose Refresh question bank to try again.')}`,
          ),
        );
      });
  }

  /* what the server's refusal means for the teacher; an invalid topic is shown on the topic itself. */
  private refusedSave(error: unknown, form: QuestionForm): Error {
    if (!(error instanceof ApiError)) return error instanceof Error ? error : new Error(String(error));
    if (error.code === 'BANK_FULL')
      return new Error(
        'Your bank holds 2,000 questions, the most it can. Delete questions you no longer use first.',
      );
    if (error.code === 'INVALID_TOPIC') {
      // the topic may have been archived elsewhere, so the picker reads the curriculum again.
      void this.curriculum.refresh().catch(() => undefined);
      form.controls.topic_id.setErrors({ invalid: true });
      form.controls.topic_id.markAsTouched();
      focusLater('bank-question-topic');
      return new Error('This topic can no longer be used. Choose an active topic.');
    }
    if (error.status === 404)
      return new Error(
        'This bank question no longer exists; it may have been deleted in another tab. Your changes are kept here.',
      );
    return error;
  }

  /* a free response's points: its rubric's highest levels added up. */
  rubricPoints(form: QuestionForm): number {
    return rubricTotal(form.controls.criteria.getRawValue());
  }

  addCriterion(): void {
    const form = this.form();
    if (form.controls.criteria.length >= MAX_CRITERIA) return;
    form.controls.criteria.push(criterionForm());
    form.markAsDirty();
    focusLater(`bank-question-criterion-${form.controls.criteria.length - 1}-title`);
  }

  removeCriterion(index: number): void {
    const form = this.form();
    if (form.controls.criteria.length <= 1) return;
    form.controls.criteria.removeAt(index);
    form.markAsDirty();
  }

  // --- deleting
  // --------------------------------------------------------------------------------

  confirmDelete(question: BankQuestion): void {
    this.deleting.set(question);
    this.deleteDialog?.show();
  }

  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.close();
  }

  /* deletes the bank question for good; assessments keep their copies. */
  remove(): void {
    const question = this.deleting();
    if (!question) return;
    void this.deleteState.run(async () => {
      await this.api.removeBankQuestion(question.id);
      this.closeDelete();
      // a clean form for the deleted question has nothing left to save.
      if (this.editKey() === question.id && !this.form().dirty) this.editKey.set('');
      this.state.error.set('');
      this.state.message.set('Bank question deleted.');
      focusLater('bank-title');
      await rereadAfterDelete(
        this.questions,
        question.id,
        () => this.reread(),
        () => true,
        this.state,
        { list: 'question bank', refresh: 'Refresh question bank' },
      );
    });
  }
}

/* a bank question that could not be opened, said in the teacher's terms. */
function refusedOpen(error: unknown): never {
  if (error instanceof ApiError && error.status === 404)
    throw new Error('This bank question no longer exists. It may have been deleted.');
  throw error;
}
