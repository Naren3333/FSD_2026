import { Component, DestroyRef, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { AssessmentApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, Node, Question } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
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

export { optionLines } from './editor';

/** How long a student's typing rests before their work is saved. */
const SAVE_DELAY = 1500;

@Component({
  selector: 'app-assessments',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
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
  readonly maxQuestions = MAX_QUESTIONS;
  readonly maxCriteria = MAX_CRITERIA;
  /** The assessment a link such as student Home asked to open, until the classroom's list has it. */
  private requested = '';
  private saveTimer: ReturnType<typeof setTimeout> | undefined;
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
    inject(UnsavedChanges).track(() => this.form.dirty || this.answers.dirty);
    this.form.controls.questions.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.typesChanged());
    this.typesChanged();
    inject(DestroyRef).onDestroy(() => clearTimeout(this.saveTimer));
    onClassroomChange(() => {
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
    if (!this.state.validate(this.form)) return;
    const status = publish ? 'published' : 'draft';
    const assessment = toAssessment(this.form, this.classroomId(), status);
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
      this.assessments.set(await this.assessmentApi.list(this.classroomId()));
    }, message);
  }

  edit(id: string): void {
    if (this.form.dirty) {
      this.state.error.set('Save or clear the assessment you are editing first.');
      return;
    }
    void this.state.run(async () => {
      const draft = await this.assessmentApi.get(id);
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
    void this.state.run(async () => {
      await this.assessmentApi.publish(item.id);
      if (this.editing()?.id === item.id) this.cancelEdit();
      this.assessments.set(await this.assessmentApi.list(this.classroomId()));
    }, `${item.title} published.`);
  }

  confirmDelete(item: AssessmentSummary): void {
    this.deleting.set(item);
    this.deleteState.error.set('');
    this.deleteDialog?.show();
  }

  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.close();
  }

  remove(): void {
    const item = this.deleting();
    if (!item) return;
    void this.deleteState.run(async () => {
      await this.assessmentApi.remove(item.id);
      if (this.editing()?.id === item.id) this.cancelEdit();
      if (this.selected()?.id === item.id) this.selected.set(null);
      this.closeDelete();
      this.assessments.set(await this.assessmentApi.list(this.classroomId()));
      this.state.message.set('Draft deleted.');
    });
  }

  // --- Opening and answering -------------------------------------------------------------------------------

  open(id: string): void {
    if (this.answers.dirty) {
      this.state.error.set('Submit your current answers before opening another assessment.');
      return;
    }
    void this.state.run(async () => {
      const assessment = await this.assessmentApi.get(id);
      const submitted = this.assessments.value()?.find((a) => a.id === id)?.submitted ?? false;
      const saved =
        this.session.isStudent() && !submitted ? (await this.assessmentApi.saved(id)).answers : {};
      clearTimeout(this.saveTimer);
      this.saveNote.set(Object.keys(saved).length ? 'Your saved answers are back.' : '');
      this.answers = new FormGroup(
        Object.fromEntries(
          assessment.questions.map((q) => [
            q.id!,
            new FormControl(saved[q.id!] ?? '', {
              nonNullable: true,
              validators: [Validators.required, Validators.pattern(/\S/), Validators.maxLength(limitOf(q))],
            }),
          ]),
        ),
      );
      if (this.session.isStudent() && !submitted)
        this.answers.valueChanges.subscribe(() => this.scheduleSave(assessment.id));
      this.selected.set(assessment);
      this.submitted.set(submitted);
    });
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
      this.assessmentApi.save(assessmentId, answers).then(
        (r) => {
          if (this.selected()?.id === assessmentId && !this.submitted())
            this.saveNote.set(`Saved at ${time(r.saved_at)}.`);
        },
        () =>
          this.saveNote.set('Your answers could not be saved. They stay here; submit when you are ready.'),
      );
    }, SAVE_DELAY);
  }

  submit(): void {
    const assessment = this.selected();
    if (!assessment || !this.state.validate(this.answers)) return;
    let late = false;
    void this.state
      .run(async () => {
        clearTimeout(this.saveTimer);
        const submission = await this.assessmentApi.submit(assessment.id, this.answers.getRawValue());
        late = submission.late;
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
    const value = this.answers.get(question.id!)?.value ?? '';
    if (question.type === 'free_response')
      return `${this.points(question)} · ${value.length} of ${limitOf(question)} characters`;
    return this.points(question);
  }
}

/** The most characters a student may give the question, as Assessment allows. */
function limitOf(question: Question): number {
  return question.type === 'free_response' ? (question.max_length ?? 5000) : 500;
}
