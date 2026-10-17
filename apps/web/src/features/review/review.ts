import { Component, DestroyRef, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, Evaluation, Evidence, Question } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { dateTime } from '../../shared/format';
import { MarksPipe } from '../../shared/marks';
import { assessmentsOf, promptOf, studentNames } from '../../shared/names';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import {
  applySuggestion,
  flagText,
  markingForm,
  marksOf,
  suggestionReason,
  type MarkingForm,
} from './marking';

/** A classroom's evaluations with the names of their students and assessments. */
interface Grades {
  grades: Evaluation[];
  students: Map<string, string>;
  assessments: Map<string, Assessment>;
}

/** How long an open grade first waits before re-reading AI suggestions still being prepared, and the longest wait.
 * Grading has no read of one evaluation, so each re-read fetches the assessment's grades (up to 500); the wait
 * doubles after every re-read that finds a suggestion still pending. */
const SUGGESTION_POLL = 5000;
const SUGGESTION_POLL_MAX = 60000;

@Component({
  selector: 'app-review',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
    MarksPipe,
  ],
  templateUrl: './review.html',
})
export class ReviewComponent {
  private readonly grading = inject(GradingApi);
  private readonly identity = inject(IdentityApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  /** Releasing an assessment's results, in its confirmation dialog. */
  readonly releaseState = new ActionState();
  readonly grades = new Section<Grades>();
  /** The classroom's published assessments, to choose one to mark. */
  readonly published = new Section<AssessmentSummary[]>();
  readonly filter = new FormControl('', { nonNullable: true });
  /** The assessment whose grades are shown; '' for the classroom's newest grades. */
  readonly assessmentId = signal('');
  /** The draft being marked. */
  readonly selected = signal<Evaluation | null>(null);
  readonly finalizing = signal(false);
  readonly releasing = signal(false);
  marks: MarkingForm = new FormGroup({});
  @ViewChild('dialog') dialog?: ConfirmDialogComponent;
  @ViewChild('releaseDialog') releaseDialog?: ConfirmDialogComponent;
  readonly form = new FormGroup({
    reason: new FormControl('', { nonNullable: true, validators: requiredText(1000) }),
  });
  /** The chosen assessment's finalized grades its students cannot see yet. */
  readonly unreleased = computed(() => {
    const id = this.assessmentId();
    return (this.grades.value()?.grades ?? []).filter(
      (g) => g.assessment_id === id && g.status === 'finalized' && !g.released_at,
    ).length;
  });
  readonly flagText = flagText;
  readonly suggestionReason = suggestionReason;
  private poll: ReturnType<typeof setTimeout> | undefined;

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.marks.dirty);
    inject(DestroyRef).onDestroy(() => clearTimeout(this.poll));
    onClassroomChange(() => {
      this.filter.setValue('');
      this.assessmentId.set('');
      this.load();
    });
  }

  load(): void {
    const classroomId = this.classroomId();
    this.grades.clear();
    this.published.clear();
    this.stopMarking();
    if (!classroomId) return;
    const assessmentId = this.assessmentId();
    void this.grades.load(() => this.read(classroomId, assessmentId));
    void this.published.load(async () =>
      (await this.assessmentApi.list(classroomId)).filter((a) => a.status === 'published'),
    );
  }

  /** Shows the grades of the assessment the filter names, or the newest of all. */
  choose(): void {
    if (this.marks.dirty) {
      this.state.error.set('Save or close the grade you are marking first.');
      this.filter.setValue(this.assessmentId());
      return;
    }
    this.assessmentId.set(this.filter.value);
    this.stopMarking();
    const classroomId = this.classroomId();
    if (classroomId) void this.grades.load(() => this.read(classroomId, this.filter.value));
  }

  /** The evaluations with the names of their students, assessments, questions and skills. */
  private async read(classroomId: string, assessmentId: string): Promise<Grades> {
    const grades = await this.grading.evaluations(classroomId, assessmentId);
    const [roster, assessments] = await Promise.all([
      this.identity.roster(classroomId),
      assessmentsOf(grades, (id) => this.assessmentApi.get(id)),
      this.curriculum.need(grades.flatMap((g) => g.evidence.map((e) => e.skill_id))),
    ]);
    return { grades, students: studentNames(roster), assessments };
  }

  /** The student's roster name, or '' for a student no longer enrolled or not yet named. */
  studentName(grade: Evaluation): string {
    return this.grades.value()?.students.get(grade.student_id) ?? '';
  }
  title(grade: Evaluation): string {
    return this.grades.value()?.assessments.get(grade.assessment_id)?.title ?? '';
  }
  prompt(grade: Evaluation, evidence: Evidence): string {
    return promptOf(this.grades.value()?.assessments ?? new Map(), grade.assessment_id, evidence.question_id);
  }
  question(grade: Evaluation, evidence: Evidence): Question | undefined {
    return this.grades
      .value()
      ?.assessments.get(grade.assessment_id)
      ?.questions.find((q) => q.id === evidence.question_id);
  }
  skill(evidence: Evidence): string {
    return this.curriculum.skillName(evidence.skill_id);
  }
  toMark(grade: Evaluation): number {
    return grade.evidence.filter((e) => e.needs_marking).length;
  }
  outcome(evidence: Evidence): string {
    if (evidence.needs_marking) return 'Needs marking';
    if (evidence.marked_by === 'teacher') return 'Marked by you';
    return evidence.correct ? 'Correct' : 'Incorrect';
  }
  submittedAt(grade: Evaluation): string {
    return grade.submitted_at ? dateTime(grade.submitted_at) : '';
  }

  // --- Marking ---------------------------------------------------------------------------------------------

  review(grade: Evaluation): void {
    if (this.marks.dirty && this.selected()?.id !== grade.id) {
      this.state.error.set('Save or close the grade you are marking first.');
      return;
    }
    const questions = this.grades.value()?.assessments.get(grade.assessment_id)?.questions ?? [];
    this.marks = markingForm(grade.evidence, questions);
    this.selected.set(grade);
    this.state.error.set('');
    this.watchSuggestions();
    setTimeout(() => document.getElementById(`marking-${grade.id}`)?.focus());
  }

  stopMarking(): void {
    clearTimeout(this.poll);
    this.marks = new FormGroup({});
    this.selected.set(null);
  }

  useSuggestion(grade: Evaluation, evidence: Evidence): void {
    const marks = this.marks.controls[evidence.question_id];
    if (marks && evidence.suggestion?.status === 'ready') applySuggestion(marks, evidence.suggestion);
  }

  levelName(grade: Evaluation, evidence: Evidence, criterionId: string, points: number): string {
    const criterion = this.question(grade, evidence)?.rubric?.criteria.find((c) => c.id === criterionId);
    const level = criterion?.levels.find((l) => l.points === points);
    return `${criterion?.title ?? 'Criterion'}: ${points} (${level?.descriptor ?? 'level'})`;
  }

  saveMarks(): void {
    const grade = this.selected();
    if (!grade) return;
    const marks = marksOf(this.marks, grade.evidence);
    if (!Object.keys(marks).length) {
      this.state.error.set('Nothing to save: no marks or feedback changed.');
      return;
    }
    void this.state.run(async () => {
      this.replace(await this.grading.mark(grade.id, marks));
    }, 'Marks saved. The grade is still a draft.');
  }

  /** Puts a re-read grade in the list and, when it is the one being marked, in the marking form. */
  private replace(grade: Evaluation): void {
    const current = this.grades.value();
    if (current)
      this.grades.set({ ...current, grades: current.grades.map((g) => (g.id === grade.id ? grade : g)) });
    if (this.selected()?.id === grade.id) {
      const questions = current?.assessments.get(grade.assessment_id)?.questions ?? [];
      this.marks = markingForm(grade.evidence, questions);
      this.selected.set(grade);
    }
  }

  /** While a suggestion is being prepared, re-reads the grade so it appears without a refresh, less often the longer
   * it takes. */
  private watchSuggestions(delay = SUGGESTION_POLL): void {
    clearTimeout(this.poll);
    const grade = this.selected();
    if (!grade?.evidence.some((e) => e.suggestion?.status === 'pending')) return;
    const classroomId = this.classroomId();
    this.poll = setTimeout(() => {
      void this.grading.evaluations(classroomId, grade.assessment_id).then(
        (rows) => {
          const fresh = rows.find((r) => r.id === grade.id);
          const current = this.selected();
          if (!fresh || current?.id !== grade.id || this.classroomId() !== classroomId) return;
          // Only the suggestions change here, so the teacher's unsaved marks stay.
          const suggestions = new Map(fresh.evidence.map((e) => [e.question_id, e.suggestion]));
          this.selected.set({
            ...current,
            evidence: current.evidence.map((e) => ({ ...e, suggestion: suggestions.get(e.question_id) })),
          });
          this.watchSuggestions(Math.min(delay * 2, SUGGESTION_POLL_MAX));
        },
        () => {
          if (this.selected()?.id === grade.id)
            this.watchSuggestions(Math.min(delay * 2, SUGGESTION_POLL_MAX));
        },
      );
    }, delay);
  }

  askFinalize(): void {
    if (!this.selected() || !this.state.validate(this.marks, 'marking-form')) return;
    this.finalizing.set(true);
    this.form.reset();
    this.dialog?.show();
  }

  close(): void {
    this.form.reset();
    this.finalizing.set(false);
    this.dialog?.close();
  }

  finalize(): void {
    const grade = this.selected();
    if (!grade || !this.state.validate(this.form, 'finalize-form')) return;
    const marks = marksOf(this.marks, grade.evidence);
    const classroomId = this.classroomId();
    const assessmentId = this.assessmentId();
    void this.state.run(
      async () => {
        await this.grading.finalize(grade.id, this.form.controls.reason.value, marks);
        this.close();
        this.stopMarking();
        await this.grades.reload(() => this.read(classroomId, assessmentId));
      },
      grade.release_policy === 'manual'
        ? 'Grade finalized. Students see it when you release the results.'
        : 'Grade finalized. Performance will update after event processing.',
    );
  }

  // --- Releasing -------------------------------------------------------------------------------------------

  askRelease(): void {
    this.releasing.set(true);
    this.releaseDialog?.show();
  }

  closeRelease(): void {
    this.releasing.set(false);
    this.releaseDialog?.close();
  }

  release(): void {
    const assessmentId = this.assessmentId();
    const classroomId = this.classroomId();
    if (!assessmentId) return;
    void this.releaseState.run(async () => {
      const result = await this.grading.release(assessmentId);
      this.closeRelease();
      await this.grades.reload(() => this.read(classroomId, assessmentId));
      this.state.message.set(
        `${result.released} ${result.released === 1 ? 'result' : 'results'} released to students.`,
      );
    });
  }

  assessmentTitle(id: string): string {
    return this.published.value()?.find((a) => a.id === id)?.title ?? 'this assessment';
  }
}
