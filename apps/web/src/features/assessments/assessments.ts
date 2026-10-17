import { Component, DestroyRef, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import type { Subscription } from 'rxjs';
import { AssessmentApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, Node, Question, SavedAnswers } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { dateTime, time } from '../../shared/format';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import {
  MAX_CRITERIA,
  MAX_QUESTIONS,
  assessmentForm,
  clearForm,
  criterionForm,
  dueState,
  fillForm,
  questionForm,
  rubricTotal,
  teacherMarked,
  toAssessment,
  type DueState,
  type QuestionForm,
} from './editor';

/** How long a student's typing rests before their work is saved. */
const SAVE_DELAY = 1500;

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
  ],
  templateUrl: './assessments.html',
})
export class AssessmentsComponent {
  readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  /** Deleting a draft, in its confirmation dialog. */
  readonly deleteState = new ActionState();
  /** Clearing the student's answers, in its confirmation dialog. */
  readonly clearState = new ActionState();
  readonly assessments = new Section<AssessmentSummary[]>();
  readonly skills = this.curriculum.skills;
  /** Reading the curriculum for the skill a new question uses. */
  readonly skillsState = new Section<Node[]>();
  readonly selected = signal<Assessment | null>(null);
  readonly submitted = signal(false);
  /** The draft the editor is changing; null while it creates a new assessment. */
  readonly editing = signal<Assessment | null>(null);
  /** The draft waiting for its deletion to be confirmed. */
  readonly deleting = signal<AssessmentSummary | null>(null);
  /** The autosave's last outcome, for the student. */
  readonly saveNote = signal('');
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  @ViewChild('clearDialog') clearDialog?: ConfirmDialogComponent;
  readonly maxQuestions = MAX_QUESTIONS;
  readonly maxCriteria = MAX_CRITERIA;
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

  load(): void {
    const classroomId = this.classroomId();
    this.assessments.clear();
    if (!classroomId) return;
    void this.assessments.load(() => this.assessmentApi.list(classroomId)).then(() => this.openRequested());
    // Read fresh, so a skill a colleague just added is there to choose.
    if (this.session.isTeacher()) void this.skillsState.load(() => this.curriculum.refresh());
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
    // A new question starts with the previous one's skill, which is usually the same.
    const skill = this.questions.at(this.questions.length - 1)?.controls.skill_id.value ?? '';
    const q = questionForm();
    q.controls.skill_id.setValue(skill);
    this.questions.push(q);
    this.form.markAsDirty();
    this.focusLater(`question-${this.questions.length - 1}-prompt`);
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

  /** Saves the editor as a draft (new or existing), or publishes it. */
  save(publish: boolean): void {
    if (!this.state.validate(this.form, 'assessment-editor')) return;
    const status = publish ? 'published' : 'draft';
    const classroomId = this.classroomId();
    const assessment = toAssessment(this.form, classroomId, status);
    const editing = this.editing();
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

  edit(id: string): void {
    if (this.form.dirty) {
      this.state.error.set('Save or clear the assessment you are editing first.');
      return;
    }
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      const draft = await this.assessmentApi.get(id);
      // The user may have moved to another classroom while the draft loaded.
      if (this.classroomId() !== classroomId) return;
      fillForm(this.form, draft);
      this.editing.set(draft);
      this.focusLater('assessment-title');
    });
  }

  cancelEdit(): void {
    clearForm(this.form);
    this.editing.set(null);
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
      await this.assessmentApi.remove(item.id);
      if (this.editing()?.id === item.id) this.cancelEdit();
      if (this.selected()?.id === item.id) this.selected.set(null);
      this.closeDelete();
      await this.assessments.reload(() => this.assessmentApi.list(classroomId));
      this.state.message.set('Draft deleted.');
    });
  }

  // --- Opening and answering -------------------------------------------------------------------------------

  /** Whether a student has answers here that are not submitted; a teacher's preview never counts. */
  answersUnsaved(): boolean {
    return this.session.isStudent() && this.answers.dirty;
  }

  open(id: string): void {
    if (this.answersUnsaved()) {
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
            new FormControl(saved[q.id] ?? '', {
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
        const submission = await this.assessmentApi.submit(assessment.id, this.answers.getRawValue());
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
    return question.points === 1 ? '1 mark' : `${question.points} marks`;
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
