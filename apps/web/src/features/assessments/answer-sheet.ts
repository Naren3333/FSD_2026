import { Component, DestroyRef, Input, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import type { Subscription } from 'rxjs';
import { AssessmentApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, OwnSubmission, Question, SavedAnswers } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { ApiError } from '../../core/gateway';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { dueState, type DueState } from '../../shared/due';
import { dateTime, time } from '../../shared/format';
import type { Section } from '../../shared/section';
import { requiredText } from '../../shared/validators';
import { marksText } from './editor';

/* how long a student's typing rests before their work is saved. */
const SAVE_DELAY = 1500;
/* how often the open assessment's due state is looked at again, so submit closes when a due date passes. */
const DUE_CHECK = 30 * 1000;
/* why a student's submission was refused when the teacher changed the assessment after the student opened it. */
const CHANGED =
  'This assessment changed since you opened it. Open it again to see the current questions; your answers to ' +
  'questions that did not change are kept.';

/* the open assessment: a student answering it (saved as they type until they submit) or reading what they submitted, or a teacher trying their preview, which is never saved. */
@Component({
  selector: 'app-answer-sheet',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, ConfirmDialogComponent],
  templateUrl: './answer-sheet.html',
})
export class AnswerSheetComponent {
  readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  /* the classroom's assessments, which say whether the student has submitted each; a submission marks it so. */
  @Input({ required: true }) assessments!: Section<AssessmentSummary[]>;
  /* the page's own state, where opening and submitting report how they went. */
  @Input({ required: true }) state!: ActionState;
  /* clearing the student's answers, in its confirmation dialog. */
  readonly clearState = new ActionState();
  readonly selected = signal<Assessment | null>(null);
  readonly submitted = signal(false);
  /* the student's own submission of the open assessment, read back once they have submitted it. */
  readonly submission = signal<OwnSubmission | null>(null);
  /* the autosave's last outcome, for the student. */
  readonly saveNote = signal('');
  /* whether the teacher changed the open assessment since the student opened it, so it must be opened again. */
  readonly changed = signal(false);
  @ViewChild('clearDialog') clearDialog?: ConfirmDialogComponent;
  private saveTimer: ReturnType<typeof setTimeout> | undefined;
  /* saves the student's answers as they type, while the open assessment still takes them. */
  private autosave: Subscription | undefined;
  /* saves run one after another, so an older save never lands after a newer one. */
  private saving: Promise<unknown> = Promise.resolve();
  answers = new FormGroup<Record<string, FormControl<string>>>({});
  /* the time the due state is judged at: now, moved on every due_check while the page is open. */
  private readonly now = signal(new Date());
  /* where the open assessment stands with its due date. */
  readonly due = computed<DueState>(() => {
    const a = this.selected();
    return dueState(a?.due_at, a?.late_policy, this.now());
  });
  readonly dateTime = dateTime;
  readonly schedule = schedule;

  constructor() {
    // a teacher's preview answers are thrown away, so only a student's answers are unsaved work.
    inject(UnsavedChanges).track(() => this.answersUnsaved());
    const dueCheck = setInterval(() => this.now.set(new Date()), DUE_CHECK);
    inject(DestroyRef).onDestroy(() => {
      this.stopAutosave();
      clearInterval(dueCheck);
    });
    onClassroomChange(() => this.close());
  }

  /* opens the teacher's preview of an assessment, or closes it when it is the one open. */
  togglePreview(id: string): void {
    if (this.selected()?.id === id) this.closePreview();
    else this.open(id);
  }

  /* closes the assessment, as when it is deleted; its answers were saved as they were typed, or (a teacher's preview) never. */
  close(): void {
    this.stopAutosave();
    this.selected.set(null);
  }

  /* closes the teacher's preview; its answers were never saved. */
  closePreview(returnFocus = false): void {
    const id = this.selected()?.id;
    this.stopAutosave();
    this.selected.set(null);
    if (returnFocus && id) this.focusLater('preview-' + id);
  }

  /* whether a student has answers here that are not submitted; a teacher's preview never counts. */
  answersUnsaved(): boolean {
    return this.session.isStudent() && this.answers.dirty;
  }

  /* opens the assessment again after the teacher changed it, keeping the student's answers to the questions that are still there. */
  reopen(): void {
    const assessment = this.selected();
    if (assessment) this.open(assessment.id, this.answers.getRawValue());
  }

  /* opens an assessment. `keep` holds answers the student has on the page, which win over their saved work. */
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
      const own = this.session.isStudent() && submitted ? await this.ownSubmission(id) : null;
      const saved = own
        ? own.answers
        : this.session.isStudent() && !submitted
          ? (await this.assessmentApi.saved(id)).answers
          : {};
      if (this.classroomId() !== classroomId) return;
      this.stopAutosave();
      this.now.set(new Date());
      this.submission.set(own);
      this.saveNote.set(!own && Object.keys(saved).length ? 'Your saved answers are back.' : '');
      this.answers = new FormGroup(
        Object.fromEntries(
          assessment.questions.map((q) => [
            q.id,
            new FormControl(keep?.[q.id] ?? saved[q.id] ?? '', {
              nonNullable: true,
              validators: requiredText(limitOf(q)),
            }),
          ]),
        ),
      );
      // submitted or closed work can only be read; a teacher's preview can be tried but is never saved.
      if (this.session.isStudent() && !answering) this.answers.disable({ emitEvent: false });
      if (answering)
        this.autosave = this.answers.valueChanges.subscribe(() => this.scheduleSave(assessment.id));
      this.selected.set(assessment);
      this.submitted.set(submitted);
      this.changed.set(false);
      // kept answers are still unsubmitted work, and those that differ from the saved work are saved now.
      if (answering && keep) {
        this.answers.markAsDirty();
        if (assessment.questions.some((q) => q.id in keep && keep[q.id] !== (saved[q.id] ?? '')))
          this.scheduleSave(assessment.id);
      }
    });
  }

  /* the student's own submitted answers, or null when assessment has none (the list was out of date). */
  private async ownSubmission(id: string): Promise<OwnSubmission | null> {
    try {
      return await this.assessmentApi.submission(id);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  }

  /* stops saving the open answers: no pending save, and typing (or a reset) no longer starts one. */
  private stopAutosave(): void {
    clearTimeout(this.saveTimer);
    this.autosave?.unsubscribe();
    this.autosave = undefined;
  }

  /* saves the student's work once their typing rests. */
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
        (error: unknown) => {
          if (!stillOpen()) return;
          if (changedError(error)) {
            // as for a submission: the questions changed, so nothing more is saved until it is opened
            // again.
            this.stopAutosave();
            this.changed.set(true);
            this.saveNote.set(CHANGED);
            return;
          }
          this.saveNote.set('Your answers could not be saved. They stay here; submit when you are ready.');
        },
      );
    }, SAVE_DELAY);
  }

  /* replaces the student's saved work, after any save still on its way. */
  private persist(assessmentId: string, answers: Record<string, string>): Promise<SavedAnswers> {
    const save = this.saving.then(
      () => this.assessmentApi.save(assessmentId, answers),
      () => this.assessmentApi.save(assessmentId, answers),
    );
    this.saving = save.catch(() => undefined);
    return save;
  }

  /* the student's clear form: their saved work goes too, so it asks first. */
  confirmClear(): void {
    if (this.session.isStudent()) {
      this.clearDialog?.show();
      return;
    }
    // a teacher's preview has nothing saved.
    this.answers.reset();
  }

  /* empties the answers and saves that, as the student asked; a failure keeps their answers on the page. */
  clearAnswers(): void {
    const assessment = this.selected();
    if (!assessment || this.submitted()) return;
    void this.clearState.run(async () => {
      clearTimeout(this.saveTimer);
      await this.persist(assessment.id, {});
      // empty, not each control's first value (the saved answer); the reset is not typing, so it starts no
      // save.
      const empty = Object.fromEntries(Object.keys(this.answers.controls).map((id) => [id, '']));
      this.answers.reset(empty, { emitEvent: false });
      this.saveNote.set('Your answers are cleared.');
      this.clearDialog?.close();
    });
  }

  submit(): void {
    const assessment = this.selected();
    if (!assessment) return;
    // the due date may have passed since the page last looked.
    this.now.set(new Date());
    if (this.due() === 'closed') {
      this.state.error.set('This assessment is past its due date and no longer takes submissions.');
      return;
    }
    if (!this.state.validate(this.answers, 'answer-form')) return;
    let late = false;
    void this.state
      .run(async () => {
        clearTimeout(this.saveTimer);
        const submission = await this.assessmentApi
          .submit(assessment.id, this.answers.getRawValue())
          .catch((error: unknown) => {
            if (!changedError(error)) throw error;
            this.changed.set(true);
            this.focusLater('reopen-assessment');
            throw new Error(CHANGED);
          });
        late = submission.late;
        // submitted answers are final: they can be read, no longer changed or saved.
        this.stopAutosave();
        this.answers.disable({ emitEvent: false });
        this.answers.markAsPristine();
        this.submitted.set(true);
        this.submission.set({
          answers: this.answers.getRawValue(),
          submitted_at: new Date().toISOString(),
          late: submission.late,
        });
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

  private focusLater(id: string): void {
    setTimeout(() => document.getElementById(id)?.focus());
  }

  points(question: Question): string {
    return marksText(question.points);
  }

  /* the help line under a student's answer: its marks, and for written answers how much is left. */
  answerHint(question: Question): string {
    const value = this.answers.get(question.id)?.value ?? '';
    if (question.type === 'free_response')
      return `${this.points(question)} · ${value.length} of ${limitOf(question)} characters`;
    return this.points(question);
  }
}

/* "due 13 oct 2026, 3:00 pm", "opens …", or '' when the assessment has no dates. */
export function schedule(item: { opens_at?: string | null; due_at?: string | null }): string {
  const parts = [];
  if (item.opens_at && new Date(item.opens_at) > new Date()) parts.push(`Opens ${dateTime(item.opens_at)}`);
  if (item.due_at) parts.push(`Due ${dateTime(item.due_at)}`);
  return parts.join(' · ');
}

/* whether the server refused the student's answers because the teacher changed the assessment meanwhile. */
function changedError(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'ASSESSMENT_CHANGED';
}

/* the most characters a student may give the question, as assessment allows. */
function limitOf(question: Question): number {
  return question.type === 'free_response' ? (question.max_length ?? 5000) : 500;
}
