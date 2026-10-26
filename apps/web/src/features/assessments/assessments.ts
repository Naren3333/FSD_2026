import { messageOf } from '../../shared/errors';
import { PagedSection } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed, toSignal } from '@angular/core/rxjs-interop';
import {
  FormArray,
  FormGroup,
  ReactiveFormsModule,
  type AbstractControl,
  type ValidatorFn,
} from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { AssessmentApi, ContentApi } from '../../api/clients';
import type {
  Assessment,
  AssessmentSummary,
  DocumentRecord,
  Excerpt,
  Question,
  Topic,
} from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { dueState } from '../../shared/due';
import { Section, rereadAfterDelete } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import {
  MAX_ASSESSMENT_EXCERPTS,
  MAX_CRITERIA,
  MAX_QUESTIONS,
  assessmentForm,
  clearForm,
  criterionForm,
  excerptLabel,
  fillForm,
  markWrittenBy,
  marksText,
  parseLevels,
  questionForm,
  rubricTotal,
  schemeTotal,
  teacherMarked,
  toAssessment,
  toDetails,
  untouched,
  usesScheme,
  type QuestionForm,
} from './editor';
import { AnswerSheetComponent, schedule } from './answer-sheet';
import { QuestionGeneratorComponent } from './generator';
import { MarkingGuidanceComponent } from './guidance';
import { MarkingPointsComponent } from './marking-points';
import { focusLater } from '../../shared/focus';
import { dateTime } from '../../shared/format';

/* why a published assessment's replacement was refused, after which the editor keeps only its details. */
const LOCKED =
  'Students have submitted this assessment, so only its title, due date and late policy can change now. Save ' +
  'again to keep those changes.';

/* a teacher's view of the page, from its address: the list, the editor (`?edit=`) or a preview (`?preview=`). */
export type AssessmentsView = 'list' | 'edit' | 'preview';

@Component({
  selector: 'app-assessments',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    RouterLink,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    IconComponent,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
    NoClassroomComponent,
    PagerComponent,
    MarkingPointsComponent,
    QuestionGeneratorComponent,
    MarkingGuidanceComponent,
    AnswerSheetComponent,
  ],
  templateUrl: './assessments.html',
})
export class AssessmentsComponent {
  readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly contentApi = inject(ContentApi);
  private readonly curriculum = inject(CurriculumNames);
  private readonly classrooms = inject(ClassroomContext);
  readonly classroomId = this.classrooms.classroomId;
  readonly classroomName = this.classrooms.classroomName;
  readonly state = new ActionState();
  /* deleting an assessment, in its confirmation dialog. */
  readonly deleteState = new ActionState();
  readonly assessments = new PagedSection<AssessmentSummary>();
  readonly topics = this.curriculum.topics;
  /* reading the curriculum for the topic a new question uses. */
  readonly topicsState = new Section<Topic[]>();
  /* the classroom's documents, read once for generation, drafted guidance and where passages came from. */
  readonly classDocs = new Section<DocumentRecord[]>();
  /* the open assessment, answered or previewed. */
  @ViewChild(AnswerSheetComponent, { static: true }) sheet?: AnswerSheetComponent;
  /* the assessment the editor is changing; null while it creates a new one. */
  readonly editing = signal<Assessment | null>(null);
  /* whether anyone had submitted the assessment being edited when the teacher opened it. */
  readonly editingSubmitted = signal(false);
  /* whether the editor changes a published assessment, which stays published. */
  readonly editingPublished = computed(() => this.editing()?.status === 'published');
  /* submitted assessments allow only title, due-date and late-policy edits. */
  readonly detailsOnly = computed(() => this.editingPublished() && this.editingSubmitted());
  /* the assessment waiting for its deletion to be confirmed. */
  readonly deleting = signal<AssessmentSummary | null>(null);
  /* whether deleting the assessment waiting for confirmation also deletes submissions and their grades. */
  readonly deletingSubmitted = computed(() => {
    const item = this.deleting();
    return !!item && submittedBy(item);
  });
  readonly submittedBy = submittedBy;
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  readonly maxQuestions = MAX_QUESTIONS;
  readonly maxCriteria = MAX_CRITERIA;
  readonly maxMarkingPoints = MAX_ASSESSMENT_EXCERPTS;
  readonly usesScheme = usesScheme;
  /* the assessment a link such as student home asked to open (or a teacher's address to edit), until the classroom's list has it. */
  private requested: { id: string; edit: boolean } | null = null;
  /* the assessment the address names to open (`?open=`, or a teacher's `?preview=`), or ''. */
  private address = '';
  /* the assessment the teacher's address names to edit (`?edit=`, `new` for a new one), or ''. */
  private editAddress = '';
  /* the address's changes, followed one after another since leaving an assessment can wait on a save. */
  private following: Promise<void> = Promise.resolve();
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly query = toSignal(this.route.queryParamMap);
  /* a student's view is always the list: their open assessment replaces it (see studentViewing). */
  readonly view = computed<AssessmentsView>(() => {
    const params = this.query();
    if (!params || !this.session.isTeacher()) return 'list';
    if (params.get('edit')) return 'edit';
    return params.get('preview') || params.get('open') ? 'preview' : 'list';
  });
  readonly form = assessmentForm();
  /* re-read on every change to the form's questions, so the ai marking option knows whether it applies. */
  private readonly questionTypes = signal(this.form.controls.questions.getRawValue().map((q) => q.type));
  readonly canSuggest = computed(() => teacherMarked(this.questionTypes().map((type) => ({ type }))));

  constructor() {
    // the open assessment's answers are tracked by the answer sheet.
    inject(UnsavedChanges).track(() => this.form.dirty);
    this.form.controls.questions.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.typesChanged());
    this.typesChanged();
    let shownClassroom = '';
    onClassroomChange((classroomId) => {
      // an assessment of the classroom left behind no longer belongs in the address; a new one may stay.
      const named = this.address || (this.editAddress !== 'new' && this.editAddress);
      if (shownClassroom && classroomId !== shownClassroom && named) {
        this.requested = null;
        this.showList();
      }
      shownClassroom = classroomId;
      this.cancelEdit();
      this.assessments.page.set(0);
      this.load();
    });
    this.route.queryParamMap.pipe(takeUntilDestroyed()).subscribe((params) => {
      const open = params.get('open') ?? '';
      const edit = params.get('edit') ?? '';
      // a teacher's `?open=` (from a link such as home) is their preview, as it always was.
      const preview = edit ? '' : params.get('preview') || open;
      this.following = this.following.then(() =>
        this.session.isTeacher() ? this.followTeacher(edit, preview) : this.follow(open),
      );
    });
    // only the list: the topics, documents and any open form stay as they are.
    onSectionShown('assessments', () => {
      const classroomId = this.classroomId();
      if (classroomId) rereadQuietly(this.assessments, () => this.readList(classroomId));
    });
  }

  private readList(classroomId: string): Promise<AssessmentSummary[]> {
    return this.assessments.readPage((offset, limit) =>
      this.assessmentApi.assessmentPage(classroomId, offset, limit),
    );
  }

  /* ai marking applies only to questions a teacher marks, so the option is off without one. */
  private typesChanged(): void {
    this.questionTypes.set(this.form.controls.questions.getRawValue().map((q) => q.type));
    const option = this.form.controls.ai_marking;
    if (this.canSuggest()) option.enable({ emitEvent: false });
    else option.disable({ emitEvent: false });
  }

  get questions() {
    return this.form.controls.questions;
  }

  /* the topic's name when it is archived, or '' for an active (or unknown) topic. */
  archivedTopic(topicId: string): string {
    return this.curriculum.archived().find((t) => t.id === topicId)?.name ?? '';
  }

  /* an archived topic is out of every picker for new questions, so a question that still has one must change it. */
  private readonly activeTopic: ValidatorFn = (control) =>
    this.archivedTopic(control.value as string) ? { archived: true } : null;

  load(): void {
    const classroomId = this.classroomId();
    this.assessments.clear();
    this.classDocs.clear();
    if (!classroomId) return;
    void this.assessments.load(() => this.readList(classroomId)).then(() => this.openRequested());
    if (this.session.isTeacher()) {
      // read fresh, so a topic a colleague just added is there to choose.
      void this.topicsState.load(() => this.curriculum.refresh());
      void this.classDocs.load(() => this.contentApi.documents(classroomId));
    }
  }

  /* a student with an assessment open sees only that assessment. */
  studentViewing(): boolean {
    return this.session.isStudent() && !!this.sheet?.selected();
  }

  /* a student opens an assessment by its address, so back returns to the list. */
  openFromList(id: string): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams: { open: id } });
  }

  /* the list's address, in place of one naming an assessment. */
  private showList(): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl: true });
  }

  /* follows the address: a student's open assessment is the one it names. */
  private async follow(open: string): Promise<void> {
    this.address = open;
    const current = this.session.isStudent() ? (this.sheet?.selected()?.id ?? '') : '';
    if (current && current === open) return;
    if (current && this.sheet) {
      if (!(await this.sheet.leave())) {
        // the answers are not saved yet, so the student stays on them.
        this.address = current;
        void this.router.navigate([], {
          relativeTo: this.route,
          queryParams: { open: current },
          replaceUrl: true,
        });
        return;
      }
      if (!open) this.revealAndFocus('open-' + current);
    }
    this.requested = open ? { id: open, edit: false } : null;
    this.openRequested();
  }

  /* follows a teacher's address: the assessment it names to edit, or to preview, or the list. */
  private async followTeacher(edit: string, preview: string): Promise<void> {
    this.address = preview;
    this.editAddress = edit;
    this.requested = null;
    const shown = this.sheet?.selected()?.id ?? '';
    if (shown && shown !== preview) this.sheet?.closePreview();
    if (edit === 'new') this.startNew();
    // the assessment already being edited stays as it is, as after create draft or back and forward.
    else if (edit && edit !== this.editing()?.id) this.requested = { id: edit, edit: true };
    else if (preview && preview !== shown) this.requested = { id: preview, edit: false };
    this.focusView(edit ? 'edit' : preview ? 'preview' : 'list');
    this.openRequested();
  }

  /* the teacher's view last followed, so focus moves only when the view does. */
  private shownView: AssessmentsView = 'list';

  /* a link, back or forward that changes the view leaves focus on the hidden view, so it starts at the new one. */
  private focusView(view: AssessmentsView): void {
    const changed = view !== this.shownView;
    this.shownView = view;
    // the preview's heading takes focus once it has opened.
    if (!changed || view === 'preview') return;
    setTimeout(() => {
      const active = document.activeElement;
      if (active && active !== document.body && !active.closest('[hidden]')) return;
      document.getElementById(view === 'edit' ? 'assessment-title' : 'new-assessment')?.focus();
    });
  }

  /* a teacher's new assessment: the form as it is unless it holds a saved assessment, which it leaves only when clean. */
  private startNew(): void {
    if (!this.editing()) return;
    if (this.form.dirty) this.refuseEdit();
    else this.cancelEdit();
  }

  /* keeps the teacher on the unsaved assessment they are editing, at its address, with why. */
  private refuseEdit(): void {
    this.state.error.set('Save or clear the assessment you are editing first.');
    this.showEditor(this.editing()?.id ?? 'new', true);
  }

  /* opens the requested assessment once it is in the current classroom's list. */
  private openRequested(): void {
    const request = this.requested;
    if (!request || !this.classroomId() || this.assessments.loading()) return;
    if (request.edit) {
      this.editRequested(request.id);
      return;
    }
    const id = request.id;
    const classroomId = this.classroomId();
    this.requested = null;
    const opened = this.assessments.value()?.some((a) => a.id === id)
      ? (this.sheet?.open(id) ?? Promise.resolve(false))
      : // a home/deep link can name an assessment on another page; the server still authorizes the read.
        this.assessmentApi.get(id).then(
          (item) => {
            if (this.classroomId() !== classroomId) return false;
            if (item.classroom_id === classroomId && this.sheet) return this.sheet.open(id);
            this.state.error.set('The assessment could not be opened.');
            return false;
          },
          (error: unknown) => {
            this.state.error.set(messageOf(error, 'The assessment could not be opened.'));
            return false;
          },
        );
    void opened.then((ok) => {
      // the error shows on the list, and opening it again asks again.
      if (!ok && this.address === id) this.showList();
      // a teacher's preview replaces the list, so it starts at its top.
      if (ok && this.address === id && this.session.isTeacher())
        setTimeout(() => {
          window.scrollTo({ top: 0 });
          document.getElementById('preview-title')?.focus();
        });
      // the user left before it arrived.
      if (ok && this.address !== id && this.sheet?.selected()?.id === id) {
        if (this.session.isStudent()) void this.sheet.leave();
        else this.sheet.closePreview();
      }
    });
  }

  /* edits the assessment a teacher's address names, with its summary from the list, which says whether students have submitted it. */
  private editRequested(id: string): void {
    const list = this.assessments.value();
    // the list has not been read yet for this classroom.
    if (list === null && !this.assessments.error()) return;
    this.requested = null;
    const item = list?.find((a) => a.id === id);
    if (this.form.dirty) {
      this.refuseEdit();
      return;
    }
    if (!item) {
      this.state.error.set('The assessment could not be opened.');
      this.showList();
      return;
    }
    void this.edit(item).then(() => {
      // it could not be read: the error shows on the list.
      if (this.editAddress === id && this.editing()?.id !== id) this.showList();
    });
  }

  // --- moving between the teacher's views
  // --------------------------------------------------------------------------------

  /* the editor's address for an assessment, or `new`; a refusal or a created draft replaces the address. */
  private showEditor(id: string, replaceUrl = false): Promise<boolean> {
    return this.router.navigate([], { relativeTo: this.route, queryParams: { edit: id }, replaceUrl });
  }

  newAssessment(): void {
    void this.showEditor('new').then(() => this.revealAndFocus('assessment-title'));
  }

  editFromList(item: AssessmentSummary): void {
    void this.showEditor(item.id);
  }

  previewFromList(id: string): void {
    void this.router.navigate([], { relativeTo: this.route, queryParams: { preview: id } });
  }

  /* the preview's own close preview: back to the list, with focus on the button that opened it. */
  previewClosed(id: string): void {
    void this.router
      .navigate([], { relativeTo: this.route, queryParams: {} })
      .then(() => this.revealAndFocus('preview-' + id));
  }

  /* back to the list after the editor is done with (`replaceUrl` after a save, so back does not reopen a cleared form). */
  private leaveEditor(replaceUrl: boolean): void {
    void this.router
      .navigate([], { relativeTo: this.route, queryParams: {}, replaceUrl })
      .then(() => this.revealAndFocus('new-assessment'));
  }

  stopEditing(): void {
    this.cancelEdit();
    this.leaveEditor(false);
  }

  /* whom the list's unsaved-changes notice names. */
  unsavedName(): string {
    return this.editing()?.title || this.form.controls.title.value.trim() || 'a new assessment';
  }

  // --- the teacher's editor
  // --------------------------------------------------------------------------------

  addQuestion(): void {
    if (this.questions.length >= MAX_QUESTIONS) return;
    // a new question starts with the previous one's topic, which is usually the same, unless it is
    // archived.
    const topic = this.questions.at(this.questions.length - 1)?.controls.topic_id.value ?? '';
    const q = questionForm();
    q.controls.topic_id.setValue(this.archivedTopic(topic) ? '' : topic);
    this.questions.push(q);
    this.form.markAsDirty();
    this.focusOn(q);
    this.revealAndFocus(`question-${this.questions.length - 1}-prompt`);
  }

  /* adds a generated draft the teacher kept, in place of the editor's one question if the teacher has not started it. */
  addGenerated(question: Omit<Question, 'id'>): void {
    const replace = this.questions.length === 1 && untouched(this.questions.at(0));
    if (!replace && this.questions.length >= MAX_QUESTIONS) return;
    const room = MAX_ASSESSMENT_EXCERPTS - (replace ? 0 : this.markingPoints());
    const scheme = question.marking_scheme ?? [];
    const kept =
      usesScheme(question.type) && scheme.length > room ? { ...question, marking_scheme: [] } : question;
    const form = questionForm(kept);
    for (const excerpt of kept.marking_scheme ?? [])
      markWrittenBy(form.controls.marking_scheme, excerpt, 'ai');
    if (replace) this.questions.setControl(0, form);
    else this.questions.push(form);
    this.form.markAsDirty();
    const position = replace ? 1 : this.questions.length;
    this.state.message.set(
      `Generated question added as question ${position}.` +
        (kept === question
          ? ''
          : ` Its marking points were left out: the assessment has ${MAX_ASSESSMENT_EXCERPTS} marking points already.`),
    );
  }

  /* the editor's question prompts, so generation leaves out near-duplicates. */
  prompts(): string[] {
    return this.questions.controls.map((q) => q.controls.prompt.value);
  }

  /* marking points across the editor's questions. */
  markingPoints(): number {
    return schemeTotal(this.questions.getRawValue());
  }

  /* where a generated question's passage is from. */
  sourceOf(excerpt: Excerpt): string {
    return excerptLabel(excerpt, this.classDocs.value());
  }

  removeQuestion(index: number): void {
    if (this.questions.length <= 1) return;
    this.questions.removeAt(index);
    this.form.markAsDirty();
    this.revealAndFocus(`question-${Math.min(index, this.questions.length - 1)}-prompt`);
  }

  addCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length >= MAX_CRITERIA) return;
    question.controls.criteria.push(criterionForm());
    this.form.markAsDirty();
    this.revealAndFocus(`question-${index}-criterion-${question.controls.criteria.length - 1}-title`);
  }

  removeCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length <= 1) return;
    question.controls.criteria.removeAt(index);
    this.form.markAsDirty();
  }

  /* a criterion's levels as the editor reads them, or null when a line cannot be read. */
  readonly parseLevels = parseLevels;

  /* the due date for the save bar, so it can be checked before publishing without scrolling to it. */
  private readonly dueValue = toSignal(this.form.controls.due_at.valueChanges, {
    initialValue: this.form.controls.due_at.value,
  });
  readonly dueLine = computed(() => {
    const due = this.dueValue();
    // a value the browser could not read as a date counts as none, rather than breaking the bar.
    const when = due ? new Date(due) : null;
    return when && !Number.isNaN(when.getTime()) ? `Due ${dateTime(when.toISOString())}` : 'No due date';
  });

  /* the editor's heading, which the page header shows. */
  readonly editorTitle = computed(() => {
    const current = this.editing();
    if (!current) return 'Create an assessment';
    return (this.editingPublished() ? 'Edit assessment: ' : 'Edit draft: ') + current.title;
  });

  /* a free response's points: its rubric's highest levels added up. */
  rubricPoints(question: QuestionForm): number {
    return rubricTotal(question.controls.criteria.getRawValue());
  }

  /* a question's marks: its own, or for a written response its rubric's. */
  marksOf(question: QuestionForm): number {
    return question.controls.type.value === 'free_response'
      ? this.rubricPoints(question)
      : Number(question.controls.points.value) || 0;
  }

  /* "3 questions · 8 marks", for the save bar. */
  size(): string {
    const count = this.questions.length;
    const marks = this.questions.controls.reduce((sum, q) => sum + this.marksOf(q), 0);
    return `${count} ${count === 1 ? 'question' : 'questions'} · ${marksText(marks)}`;
  }

  readonly marksText = marksText;

  /* the outline's line for a question: the start of its prompt. */
  promptLine(question: QuestionForm): string {
    const prompt = question.controls.prompt.value.trim().split('\n')[0] ?? '';
    if (!prompt) return 'Untitled question';
    return prompt.length > 60 ? prompt.slice(0, 57).trimEnd() + '…' : prompt;
  }

  typeName(type: string): string {
    return TYPE_NAMES[type] ?? type;
  }

  /* a question's topic name, archived ones included, or ''. */
  topicName(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  /* whether the teacher has touched a control of the question that is invalid, for the outline's error dot. */
  hasErrors(question: QuestionForm): boolean {
    return invalidControl(question, true);
  }

  /* questions shown as their header only; collapsing keeps everything typed. */
  readonly collapsed = signal<ReadonlySet<QuestionForm>>(new Set());

  isCollapsed(question: QuestionForm): boolean {
    return this.collapsed().has(question);
  }

  toggleCollapsed(question: QuestionForm): void {
    const next = new Set(this.collapsed());
    if (!next.delete(question)) next.add(question);
    this.collapsed.set(next);
  }

  /* opens one question and folds the others, except any with an error, so a long assessment stays one screen per question. */
  private focusOn(question: QuestionForm): void {
    this.collapsed.set(
      new Set(this.questions.controls.filter((q) => q !== question && !invalidControl(q, false))),
    );
  }

  private expand(questions: readonly QuestionForm[]): void {
    const next = new Set(this.collapsed());
    for (const q of questions) next.delete(q);
    this.collapsed.set(next);
  }

  /* the outline's row: the question, opened, with its prompt focused. */
  goToQuestion(index: number): void {
    const question = this.questions.at(index);
    if (!question) return;
    this.focusOn(question);
    setTimeout(() => {
      document.getElementById(`question-${index}`)?.scrollIntoView({ block: 'start' });
      document.getElementById(`question-${index}-prompt`)?.focus({ preventScroll: true });
    });
  }

  /* the outline's last row: the schedule and grading settings, with their first control focused. */
  goToSchedule(): void {
    setTimeout(() => {
      document.getElementById('schedule-grading')?.scrollIntoView({ block: 'start' });
      document
        .getElementById('schedule-grading')
        ?.querySelector<HTMLElement>('input, select')
        ?.focus({ preventScroll: true });
    });
  }

  /* the ai drafting panel: closed means hidden, so its drafts and a generation on its way stay. */
  readonly generatorOpen = signal(false);

  toggleGenerator(): void {
    if (this.generatorOpen()) {
      this.closeGenerator();
      return;
    }
    this.generatorOpen.set(true);
    this.revealAndFocus('generator-heading');
  }

  closeGenerator(): void {
    this.generatorOpen.set(false);
    this.revealAndFocus('generator-toggle');
  }

  private revealAndFocus(id: string): void {
    // a question focused this way is opened first.
    const question = /^question-(\d+)-/.exec(id);
    if (question) {
      const q = this.questions.at(Number(question[1]));
      if (q) this.expand([q]);
    }
    focusLater(id);
  }

  /* saves the editor as a draft (new or existing), publishes it, or saves the changes to a published assessment (only its details once students have submitted it). */
  save(publish: boolean): void {
    // validation focuses the first error it can see, so a collapsed question with one opens first.
    this.expand(this.questions.controls.filter((q) => invalidControl(q, false)));
    if (!this.state.validate(this.form, 'assessment-editor')) return;
    const editing = this.editing();
    if (editing && this.editingPublished()) {
      this.saveChanges(editing);
      return;
    }
    const status = publish ? 'published' : 'draft';
    const classroomId = this.classroomId();
    const assessment = toAssessment(this.form, classroomId, status);
    const message = publish ? 'Assessment published.' : editing ? 'Draft saved.' : 'Draft created.';
    void this.state.run(async () => {
      const saved = editing
        ? await this.assessmentApi.update(editing.id, assessment)
        : await this.assessmentApi.create(assessment);
      if (publish) {
        this.cancelEdit();
        this.leaveEditor(true);
      } else {
        this.editing.set(saved);
        this.form.markAsPristine();
        // the new draft's own address, in place of the new assessment's.
        if (!editing) void this.showEditor(saved.id, true);
      }
      await this.assessments.reload(() => this.readList(classroomId));
    }, message);
  }

  /* saves the editor's changes to a published assessment. */
  private saveChanges(editing: Assessment): void {
    const classroomId = this.classroomId();
    const detailsOnly = this.detailsOnly();
    void this.state.run(async () => {
      try {
        if (detailsOnly) await this.assessmentApi.details(editing.id, toDetails(this.form));
        else await this.assessmentApi.update(editing.id, toAssessment(this.form, classroomId, 'published'));
      } catch (error) {
        const refused = hasSubmissions(error);
        await this.assessments.reload(() => this.readList(classroomId)).catch(() => undefined);
        const now = this.assessments.value()?.find((a) => a.id === editing.id);
        if (this.editing()?.id === editing.id && (refused || (now && submittedBy(now)))) this.lockTo(true);
        throw refused ? new Error(LOCKED) : error;
      }
      this.cancelEdit();
      this.leaveEditor(true);
      await this.assessments.reload(() => this.readList(classroomId));
    }, `${this.form.controls.title.value} saved.`);
  }

  edit(item: AssessmentSummary): Promise<void> {
    if (this.form.dirty) {
      this.refuseEdit();
      return Promise.resolve();
    }
    const classroomId = this.classroomId();
    return this.state.run(async () => {
      const assessment = await this.assessmentApi.get(item.id);
      // know which of its topics are archived; without the curriculum, the server still refuses one.
      await this.curriculum.need(assessment.questions.map((q) => q.topic_id)).catch(() => undefined);
      // the user may have moved to another classroom while the assessment loaded.
      if (this.classroomId() !== classroomId) return;
      // whole again, in case the last assessment edited was locked to its details.
      this.form.enable({ emitEvent: false });
      fillForm(this.form, assessment);
      for (const q of this.questions.controls) {
        const topic = q.controls.topic_id;
        topic.addValidators(this.activeTopic);
        topic.updateValueAndValidity({ emitEvent: false });
        // say at once which questions need another topic.
        if (topic.invalid) topic.markAsTouched();
      }
      this.editing.set(assessment);
      const firstQuestion = this.questions.at(0);
      if (firstQuestion) this.focusOn(firstQuestion);
      this.typesChanged();
      this.lockTo(submittedBy(item));
      this.revealAndFocus('assessment-title');
    });
  }

  /* records whether anyone has submitted the assessment being edited and, once anyone has, disables everything but its details, so the form checks and sends only those. */
  private lockTo(submitted: boolean): void {
    this.editingSubmitted.set(submitted);
    if (!this.detailsOnly()) return;
    const c = this.form.controls;
    for (const control of [c.questions, c.opens_at, c.release_policy, c.ai_marking])
      control.disable({ emitEvent: false });
  }

  cancelEdit(): void {
    this.form.enable({ emitEvent: false });
    clearForm(this.form);
    this.editing.set(null);
    this.editingSubmitted.set(false);
    this.typesChanged();
  }

  publish(item: AssessmentSummary): void {
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      await this.assessmentApi.publish(item.id);
      if (this.editing()?.id === item.id) this.cancelEdit();
      await this.assessments.reload(() => this.readList(classroomId));
    }, `${item.title} published.`);
  }

  confirmDelete(item: AssessmentSummary): void {
    this.deleting.set(item);
    this.deleteDialog?.show();
  }

  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.close();
  }

  remove(): void {
    const item = this.deleting();
    const classroomId = this.classroomId();
    if (!item) return;
    void this.deleteState.run(async () => {
      try {
        await this.assessmentApi.remove(item.id, submittedBy(item));
      } catch (error) {
        // a student may have submitted since the list was read: the dialog then says what deleting now
        // takes.
        const refused = hasSubmissions(error);
        const reread = await this.assessments
          .reload(() => this.readList(classroomId))
          .then(
            () => true,
            () => false,
          );
        const now = (reread && this.assessments.value()?.find((a) => a.id === item.id)) || item;
        // without a fresh count, who submitted is unknown: the dialog says so rather than guessing.
        const counted = reread ? now : { ...now, submissions: undefined };
        if (this.deleting()?.id === item.id)
          this.deleting.set(refused ? { ...counted, has_submissions: true } : now);
        throw error;
      }
      if (this.editing()?.id === item.id) this.cancelEdit();
      if (this.sheet?.selected()?.id === item.id) this.sheet.close();
      this.closeDelete();
      this.state.error.set('');
      this.state.message.set(item.status === 'draft' ? 'Draft deleted.' : `${item.title} deleted.`);
      // the dialog is closed, so a failure is reported on the page, without the deleted assessment.
      await rereadAfterDelete(
        this.assessments,
        item.id,
        () => this.assessments.reload(() => this.readList(classroomId)),
        () => this.classroomId() === classroomId,
        this.state,
        { list: 'list', refresh: 'Refresh assessments' },
      );
    });
  }

  // --- labels
  // ----------------------------------------------------------------------------------------------

  readonly schedule = schedule;

  dueBadge(item: AssessmentSummary): string {
    if (item.status === 'draft' || item.submitted) return '';
    const state = dueState(item.due_at, item.late_policy);
    return state === 'late' ? 'Overdue' : state === 'closed' ? 'Closed' : '';
  }
}

const TYPE_NAMES: Record<string, string> = {
  numerical: 'Numerical',
  multiple_choice: 'Multiple choice',
  short_answer: 'Short answer',
  free_response: 'Written response',
};

/* whether an enabled control in `control` is invalid (and, with `touched`, touched). */
function invalidControl(control: AbstractControl, touched: boolean): boolean {
  if (control.disabled) return false;
  if (control instanceof FormGroup || control instanceof FormArray)
    return Object.values(control.controls as Record<string, AbstractControl>).some((c) =>
      invalidControl(c, touched),
    );
  return control.invalid && (!touched || control.touched);
}

/* whether anyone has submitted the assessment, students since removed from the classroom included. */
function submittedBy(item: AssessmentSummary): boolean {
  return item.has_submissions ?? !!item.submissions;
}

/* whether the server refused a change because students have submitted the assessment. */
function hasSubmissions(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'HAS_SUBMISSIONS';
}
