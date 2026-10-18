import { Component, DestroyRef, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators, type ValidatorFn } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import type { Subscription } from 'rxjs';
import { AssessmentApi, ContentApi } from '../../api/clients';
import type {
  Assessment,
  AssessmentSummary,
  DocumentRecord,
  Excerpt,
  Question,
  SavedAnswers,
  Topic,
} from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { messageOf } from '../../shared/errors';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { dateTime, time } from '../../shared/format';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import {
  MAX_ASSESSMENT_EXCERPTS,
  MAX_CRITERIA,
  MAX_QUESTIONS,
  assessmentForm,
  clearForm,
  criterionForm,
  dueState,
  excerptLabel,
  fillForm,
  markWrittenBy,
  marksText,
  questionForm,
  rubricTotal,
  schemeTotal,
  teacherMarked,
  toAssessment,
  toDetails,
  untouched,
  usesScheme,
  type DueState,
  type QuestionForm,
} from './editor';
import { QuestionGeneratorComponent } from './generator';
import { MarkingGuidanceComponent } from './guidance';
import { MarkingPointsComponent } from './marking-points';

/** How long a student's typing rests before their work is saved. */
const SAVE_DELAY = 1500;
/** Why a student's submission was refused when the teacher changed the assessment after the student opened it. */
const CHANGED =
  'This assessment changed since you opened it. Open it again to see the current questions; your answers to ' +
  'questions that did not change are kept.';

@Component({
  selector: 'app-assessments',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
    MarkingPointsComponent,
    QuestionGeneratorComponent,
    MarkingGuidanceComponent,
  ],
  templateUrl: './assessments.html',
})
export class AssessmentsComponent {
  readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly contentApi = inject(ContentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  /** Deleting an assessment, in its confirmation dialog. */
  readonly deleteState = new ActionState();
  /** Clearing the student's answers, in its confirmation dialog. */
  readonly clearState = new ActionState();
  readonly assessments = new Section<AssessmentSummary[]>();
  readonly topics = this.curriculum.topics;
  /** Reading the curriculum for the topic a new question uses. */
  readonly topicsState = new Section<Topic[]>();
  /** The classroom's documents, read once for generation, drafted guidance and where passages came from. */
  readonly classDocs = new Section<DocumentRecord[]>();
  readonly selected = signal<Assessment | null>(null);
  readonly submitted = signal(false);
  /** The assessment the editor is changing; null while it creates a new one. */
  readonly editing = signal<Assessment | null>(null);
  /** How many students had submitted the assessment being edited when the teacher opened it. */
  readonly editingSubmissions = signal(0);
  /** Whether the editor changes a published assessment, which stays published. */
  readonly editingPublished = computed(() => this.editing()?.status === 'published');
  /** Whether students have submitted the published assessment being edited, so only its title, due date and late
   * policy can change: its questions and grading settings are what their answers are graded against. */
  readonly detailsOnly = computed(() => this.editingPublished() && this.editingSubmissions() > 0);
  /** The assessment waiting for its deletion to be confirmed. */
  readonly deleting = signal<AssessmentSummary | null>(null);
  /** The autosave's last outcome, for the student. */
  readonly saveNote = signal('');
  /** Whether the teacher changed the open assessment since the student opened it, so it must be opened again. */
  readonly changed = signal(false);
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  @ViewChild('clearDialog') clearDialog?: ConfirmDialogComponent;
  readonly maxQuestions = MAX_QUESTIONS;
  readonly maxCriteria = MAX_CRITERIA;
  readonly maxMarkingPoints = MAX_ASSESSMENT_EXCERPTS;
  readonly usesScheme = usesScheme;
  /** The assessment a link such as student Home asked to open, until the classroom's list has it. */
  private requested = '';
  private saveTimer: ReturnType<typeof setTimeout> | undefined;
  /** Saves the student's answers as they type, while the open assessment still takes them. */
  private autosave: Subscription | undefined;
  /** Saves run one after another, so an older save never lands after a newer one. */
  private saving: Promise<unknown> = Promise.resolve();
  answers = new FormGroup<Record<string, FormControl<string>>>({});
  readonly form = assessmentForm();
  /** Re-read on every change to the form's questions, so the AI marking option knows whether it applies. */
  private readonly questionTypes = signal(this.form.controls.questions.getRawValue().map((q) => q.type));
  readonly canSuggest = computed(() => teacherMarked(this.questionTypes().map((type) => ({ type }))));
  /** Where the open assessment stands with its due date. */
  readonly due = computed<DueState>(() => {
    const a = this.selected();
    return dueState(a?.due_at, a?.late_policy);
  });

  constructor() {
    // A teacher's preview answers are thrown away, so only a student's answers are unsaved work.
    inject(UnsavedChanges).track(() => this.form.dirty || this.answersUnsaved());
    this.form.controls.questions.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.typesChanged());
    this.typesChanged();
    inject(DestroyRef).onDestroy(() => this.stopAutosave());
    onClassroomChange(() => {
      this.stopAutosave();
      this.selected.set(null);
      this.cancelEdit();
      this.load();
    });
    inject(ActivatedRoute)
      .queryParamMap.pipe(takeUntilDestroyed())
      .subscribe((params) => {
        this.requested = params.get('open') ?? '';
        this.openRequested();
      });
  }

  /** AI marking applies only to questions a teacher marks, so the option is off without one. */
  private typesChanged(): void {
    this.questionTypes.set(this.form.controls.questions.getRawValue().map((q) => q.type));
    const option = this.form.controls.ai_marking;
    if (this.canSuggest()) option.enable({ emitEvent: false });
    else option.disable({ emitEvent: false });
  }

  get questions() {
    return this.form.controls.questions;
  }

  /** The topic's name when it is archived, or '' for an active (or unknown) topic. */
  archivedTopic(topicId: string): string {
    return this.curriculum.archived().find((t) => t.id === topicId)?.name ?? '';
  }

  /** An archived topic is out of every picker for new questions, so a question that still has one must change it. */
  private readonly activeTopic: ValidatorFn = (control) =>
    this.archivedTopic(control.value as string) ? { archived: true } : null;

  load(): void {
    const classroomId = this.classroomId();
    this.assessments.clear();
    this.classDocs.clear();
    if (!classroomId) return;
    void this.assessments.load(() => this.assessmentApi.list(classroomId)).then(() => this.openRequested());
    if (this.session.isTeacher()) {
      // Read fresh, so a topic a colleague just added is there to choose.
      void this.topicsState.load(() => this.curriculum.refresh());
      void this.classDocs.load(() => this.contentApi.documents(classroomId));
    }
  }

  /** Opens the requested assessment once it is in the current classroom's list. */
  private openRequested(): void {
    if (!this.requested || !this.assessments.value()?.some((a) => a.id === this.requested)) return;
    const id = this.requested;
    this.requested = '';
    this.open(id);
  }

  // --- The teacher's editor --------------------------------------------------------------------------------

  addQuestion(): void {
    if (this.questions.length >= MAX_QUESTIONS) return;
    // A new question starts with the previous one's topic, which is usually the same, unless it is archived.
    const topic = this.questions.at(this.questions.length - 1)?.controls.topic_id.value ?? '';
    const q = questionForm();
    q.controls.topic_id.setValue(this.archivedTopic(topic) ? '' : topic);
    this.questions.push(q);
    this.form.markAsDirty();
    this.focusLater(`question-${this.questions.length - 1}-prompt`);
  }

  /** Adds a generated draft the teacher kept, in place of the editor's one question if the teacher has not started
   * it. Its marking points are left out when the assessment already has all the marking points it can. */
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

  /** The editor's question prompts, so generation leaves out near-duplicates. */
  prompts(): string[] {
    return this.questions.controls.map((q) => q.controls.prompt.value);
  }

  /** Marking points across the editor's questions. */
  markingPoints(): number {
    return schemeTotal(this.questions.getRawValue());
  }

  /** Where a generated question's passage is from. */
  sourceOf(excerpt: Excerpt): string {
    return excerptLabel(excerpt, this.classDocs.value());
  }

  removeQuestion(index: number): void {
    if (this.questions.length <= 1) return;
    this.questions.removeAt(index);
    this.form.markAsDirty();
    this.focusLater(`question-${Math.min(index, this.questions.length - 1)}-prompt`);
  }

  addCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length >= MAX_CRITERIA) return;
    question.controls.criteria.push(criterionForm());
    this.form.markAsDirty();
    this.focusLater(`question-${index}-criterion-${question.controls.criteria.length - 1}-title`);
  }

  removeCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length <= 1) return;
    question.controls.criteria.removeAt(index);
    this.form.markAsDirty();
  }

  /** A free response's points: its rubric's highest levels added up. */
  rubricPoints(question: QuestionForm): number {
    return rubricTotal(question.controls.criteria.getRawValue());
  }

  private focusLater(id: string): void {
    setTimeout(() => document.getElementById(id)?.focus());
  }

  /** Saves the editor as a draft (new or existing), publishes it, or saves the changes to a published assessment
   * (only its details once students have submitted it). */
  save(publish: boolean): void {
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
      if (publish) this.cancelEdit();
      else {
        this.editing.set(saved);
        this.form.markAsPristine();
      }
      await this.assessments.reload(() => this.assessmentApi.list(classroomId));
    }, message);
  }

  /** Saves the editor's changes to a published assessment. When a student submitted it while the teacher was
   * editing, the replacement is refused; the editor then keeps only what can still change. */
  private saveChanges(editing: Assessment): void {
    const classroomId = this.classroomId();
    const detailsOnly = this.detailsOnly();
    void this.state.run(async () => {
      try {
        if (detailsOnly) await this.assessmentApi.details(editing.id, toDetails(this.form));
        else await this.assessmentApi.update(editing.id, toAssessment(this.form, classroomId, 'published'));
      } catch (error) {
        await this.assessments.reload(() => this.assessmentApi.list(classroomId));
        const submissions = this.assessments.value()?.find((a) => a.id === editing.id)?.submissions ?? 0;
        if (this.editing()?.id === editing.id) this.lockTo(submissions);
        throw error;
      }
      this.cancelEdit();
      await this.assessments.reload(() => this.assessmentApi.list(classroomId));
    }, `${this.form.controls.title.value} saved.`);
  }

  edit(item: AssessmentSummary): void {
    if (this.form.dirty) {
      this.state.error.set('Save or clear the assessment you are editing first.');
      return;
    }
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      const assessment = await this.assessmentApi.get(item.id);
      // Know which of its topics are archived; without the curriculum, the server still refuses one.
      await this.curriculum.need(assessment.questions.map((q) => q.topic_id)).catch(() => undefined);
      // The user may have moved to another classroom while the assessment loaded.
      if (this.classroomId() !== classroomId) return;
      // Whole again, in case the last assessment edited was locked to its details.
      this.form.enable({ emitEvent: false });
      fillForm(this.form, assessment);
      for (const q of this.questions.controls) {
        const topic = q.controls.topic_id;
        topic.addValidators(this.activeTopic);
        topic.updateValueAndValidity({ emitEvent: false });
        // Say at once which questions need another topic.
        if (topic.invalid) topic.markAsTouched();
      }
      this.editing.set(assessment);
      this.typesChanged();
      this.lockTo(item.submissions ?? 0);
      this.focusLater('assessment-title');
    });
  }

  /** Records how many students have submitted the assessment being edited and, once any has, disables everything
   * but its details, so the form checks and sends only those. */
  private lockTo(submissions: number): void {
    this.editingSubmissions.set(submissions);
    if (!this.detailsOnly()) return;
    const c = this.form.controls;
    for (const control of [c.questions, c.opens_at, c.release_policy, c.ai_marking])
      control.disable({ emitEvent: false });
  }

  cancelEdit(): void {
    this.form.enable({ emitEvent: false });
    clearForm(this.form);
    this.editing.set(null);
    this.editingSubmissions.set(0);
    this.typesChanged();
  }

  publish(item: AssessmentSummary): void {
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      await this.assessmentApi.publish(item.id);
      if (this.editing()?.id === item.id) this.cancelEdit();
      await this.assessments.reload(() => this.assessmentApi.list(classroomId));
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
        await this.assessmentApi.remove(item.id, !!item.submissions);
      } catch (error) {
        // A student may have submitted since the list was read: the dialog then says what deleting now takes.
        await this.assessments.reload(() => this.assessmentApi.list(classroomId));
        const now = this.assessments.value()?.find((a) => a.id === item.id);
        if (now && this.deleting()?.id === item.id) this.deleting.set(now);
        throw error;
      }
      if (this.editing()?.id === item.id) this.cancelEdit();
      if (this.selected()?.id === item.id) this.selected.set(null);
      this.closeDelete();
      this.state.error.set('');
      this.state.message.set(item.status === 'draft' ? 'Draft deleted.' : `${item.title} deleted.`);
      try {
        await this.assessments.reload(() => this.assessmentApi.list(classroomId));
      } catch (error) {
        // The dialog is closed, so the page says the list is out of date, without the deleted assessment.
        const list = this.assessments.value();
        if (list && this.classroomId() === classroomId)
          this.assessments.set(list.filter((a) => a.id !== item.id));
        this.state.error.set(
          `The list could not be refreshed. ${messageOf(error, 'Choose Refresh assessments to try again.')}`,
        );
      }
    });
  }

  // --- Opening and answering -------------------------------------------------------------------------------

  /** Opens the teacher's preview of an assessment, or closes it when it is the one open. */
  togglePreview(id: string): void {
    if (this.selected()?.id === id) this.closePreview();
    else this.open(id);
  }

  /** Closes the teacher's preview; its answers were never saved. From the preview's own button, focus goes back to
   * the button in the list that opened it. */
  closePreview(returnFocus = false): void {
    const id = this.selected()?.id;
    this.stopAutosave();
    this.selected.set(null);
    if (returnFocus && id) this.focusLater('preview-' + id);
  }

  /** Whether a student has answers here that are not submitted; a teacher's preview never counts. */
  answersUnsaved(): boolean {
    return this.session.isStudent() && this.answers.dirty;
  }

  /** Opens the assessment again after the teacher changed it, keeping the student's answers to the questions that are
   * still there. */
  reopen(): void {
    const assessment = this.selected();
    if (assessment) this.open(assessment.id, this.answers.getRawValue());
  }

  /** Opens an assessment. `keep` holds answers the student has on the page, which win over their saved work. */
  open(id: string, keep?: Record<string, string>): void {
    if (!keep && this.answersUnsaved()) {
      this.state.error.set('Submit your current answers before opening another assessment.');
      return;
    }
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      const assessment = await this.assessmentApi.get(id);
      const submitted = this.assessments.value()?.find((a) => a.id === id)?.submitted ?? false;
      const closed = dueState(assessment.due_at, assessment.late_policy) === 'closed';
      const answering = this.session.isStudent() && !submitted && !closed;
      const saved =
        this.session.isStudent() && !submitted ? (await this.assessmentApi.saved(id)).answers : {};
      if (this.classroomId() !== classroomId) return;
      this.stopAutosave();
      this.saveNote.set(Object.keys(saved).length ? 'Your saved answers are back.' : '');
      this.answers = new FormGroup(
        Object.fromEntries(
          assessment.questions.map((q) => [
            q.id,
            new FormControl(keep?.[q.id] ?? saved[q.id] ?? '', {
              nonNullable: true,
              validators: [Validators.required, Validators.pattern(/\S/), Validators.maxLength(limitOf(q))],
            }),
          ]),
        ),
      );
      // Submitted or closed work can only be read; a teacher's preview can be tried but is never saved.
      if (this.session.isStudent() && !answering) this.answers.disable({ emitEvent: false });
      if (answering)
        this.autosave = this.answers.valueChanges.subscribe(() => this.scheduleSave(assessment.id));
      this.selected.set(assessment);
      this.submitted.set(submitted);
      this.changed.set(false);
      // Kept answers are still unsubmitted work, and those that differ from the saved work are saved now.
      if (answering && keep) {
        this.answers.markAsDirty();
        if (assessment.questions.some((q) => q.id in keep && keep[q.id] !== (saved[q.id] ?? '')))
          this.scheduleSave(assessment.id);
      }
    });
  }

  /** Stops saving the open answers: no pending save, and typing (or a reset) no longer starts one. */
  private stopAutosave(): void {
    clearTimeout(this.saveTimer);
    this.autosave?.unsubscribe();
    this.autosave = undefined;
  }

  /** Saves the student's work once their typing rests. A failure keeps the answers and says so. */
  private scheduleSave(assessmentId: string): void {
    clearTimeout(this.saveTimer);
    this.saveNote.set('Saving…');
    this.saveTimer = setTimeout(() => {
      if (this.selected()?.id !== assessmentId || this.submitted()) return;
      const answers = Object.fromEntries(
        Object.entries(this.answers.getRawValue()).filter(([, answer]) => answer !== ''),
      );
      const stillOpen = () => this.selected()?.id === assessmentId && !this.submitted();
      this.persist(assessmentId, answers).then(
        (r) => {
          if (stillOpen()) this.saveNote.set(r.saved_at ? `Saved at ${time(r.saved_at)}.` : 'Saved.');
        },
        () => {
          if (stillOpen())
            this.saveNote.set('Your answers could not be saved. They stay here; submit when you are ready.');
        },
      );
    }, SAVE_DELAY);
  }

  /** Replaces the student's saved work, after any save still on its way. */
  private persist(assessmentId: string, answers: Record<string, string>): Promise<SavedAnswers> {
    const save = this.saving.then(
      () => this.assessmentApi.save(assessmentId, answers),
      () => this.assessmentApi.save(assessmentId, answers),
    );
    this.saving = save.catch(() => undefined);
    return save;
  }

  /** The student's Clear form: their saved work goes too, so it asks first. */
  confirmClear(): void {
    if (this.session.isStudent()) {
      this.clearDialog?.show();
      return;
    }
    // A teacher's preview has nothing saved.
    this.answers.reset();
  }

  /** Empties the answers and saves that, as the student asked; a failure keeps their answers on the page. */
  clearAnswers(): void {
    const assessment = this.selected();
    if (!assessment || this.submitted()) return;
    void this.clearState.run(async () => {
      clearTimeout(this.saveTimer);
      await this.persist(assessment.id, {});
      // Empty, not each control's first value (the saved answer); the reset is not typing, so it starts no save.
      const empty = Object.fromEntries(Object.keys(this.answers.controls).map((id) => [id, '']));
      this.answers.reset(empty, { emitEvent: false });
      this.saveNote.set('Your answers are cleared.');
      this.clearDialog?.close();
    });
  }

  submit(): void {
    const assessment = this.selected();
    if (!assessment || !this.state.validate(this.answers, 'answer-form')) return;
    let late = false;
    void this.state
      .run(async () => {
        clearTimeout(this.saveTimer);
        const submission = await this.assessmentApi
          .submit(assessment.id, this.answers.getRawValue())
          .catch((error: unknown) => {
            if (!(error instanceof ApiError && error.code === 'ASSESSMENT_CHANGED')) throw error;
            this.changed.set(true);
            this.focusLater('reopen-assessment');
            throw new Error(CHANGED);
          });
        late = submission.late;
        // Submitted answers are final: they can be read, no longer changed or saved.
        this.stopAutosave();
        this.answers.disable({ emitEvent: false });
        this.answers.markAsPristine();
        this.submitted.set(true);
        this.saveNote.set('');
        const list = this.assessments.value();
        if (list)
          this.assessments.set(list.map((a) => (a.id === assessment.id ? { ...a, submitted: true } : a)));
      }, 'Answers submitted. Your teacher will review the grade.')
      .then(() => {
        if (late)
          this.state.message.set('Answers submitted after the due date. Your teacher will see it was late.');
      });
  }

  // --- Labels ----------------------------------------------------------------------------------------------

  /** "Due 13 Oct 2026, 3:00 pm", "Opens …", or '' when the assessment has no dates. */
  schedule(item: { opens_at?: string | null; due_at?: string | null }): string {
    const parts = [];
    if (item.opens_at && new Date(item.opens_at) > new Date()) parts.push(`Opens ${dateTime(item.opens_at)}`);
    if (item.due_at) parts.push(`Due ${dateTime(item.due_at)}`);
    return parts.join(' · ');
  }

  dueBadge(item: AssessmentSummary): string {
    if (item.status === 'draft' || item.submitted) return '';
    const state = dueState(item.due_at, item.late_policy);
    return state === 'late' ? 'Overdue' : state === 'closed' ? 'Closed' : '';
  }

  points(question: Question): string {
    return marksText(question.points);
  }

  /** The help line under a student's answer: its marks, and for written answers how much is left. */
  answerHint(question: Question): string {
    const value = this.answers.get(question.id)?.value ?? '';
    if (question.type === 'free_response')
      return `${this.points(question)} · ${value.length} of ${limitOf(question)} characters`;
    return this.points(question);
  }
}

/** The most characters a student may give the question, as Assessment allows. */
function limitOf(question: Question): number {
  return question.type === 'free_response' ? (question.max_length ?? 5000) : 500;
}
