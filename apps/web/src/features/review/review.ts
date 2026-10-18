import { Component, DestroyRef, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { messageOf } from '../../shared/errors';
import { dateTime } from '../../shared/format';
import { MarksPipe } from '../../shared/marks';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { EvidenceNames, assessmentsOf, studentNames } from '../../shared/names';
import { PagerComponent } from '../../shared/pager';
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

/** One page of a classroom's evaluations with the names of their students and assessments, how many there are in
 * all, and, for one assessment, how many of its finalized grades its students cannot see yet. */
interface Grades {
  grades: Evaluation[];
  /** The page these are, counted from 0. */
  page: number;
  total: number;
  unreleased: number;
  students: Map<string, string>;
  assessments: Map<string, Assessment>;
}

/** How many grades one page shows; each carries the student's answers. */
const PAGE_SIZE = 20;

/** How long an open grade first waits before re-reading AI suggestions still being prepared, and the longest wait.
 * The wait doubles after every re-read that finds a suggestion still pending. */
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
    PagerComponent,
    NoClassroomComponent,
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
  readonly names = new EvidenceNames(
    () => this.grades.value()?.assessments,
    (id) => this.curriculum.topicName(id),
  );
  /** The classroom's published assessments, to choose one to mark. */
  readonly published = new Section<AssessmentSummary[]>();
  readonly filter = new FormControl('', { nonNullable: true });
  /** The assessment whose grades are shown; '' for the classroom's newest grades. */
  readonly assessmentId = signal('');
  /** The page of grades asked for, counted from 0. */
  readonly page = signal(0);
  readonly pageSize = PAGE_SIZE;
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
  /** The chosen assessment's finalized grades its enrolled students cannot see yet, across every page: what a
   * release releases. */
  readonly unreleased = computed(() => (this.assessmentId() ? (this.grades.value()?.unreleased ?? 0) : 0));
  readonly flagText = flagText;
  readonly suggestionReason = suggestionReason;
  private poll: ReturnType<typeof setTimeout> | undefined;
  /** The classroom's students' names, read once per classroom (or Refresh) rather than for every page. */
  private names$: { classroomId: string; names: Promise<Map<string, string>> } | null = null;

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.marks.dirty);
    inject(DestroyRef).onDestroy(() => clearTimeout(this.poll));
    onClassroomChange(() => {
      this.filter.setValue('');
      this.assessmentId.set('');
      this.page.set(0);
      this.load();
    });
  }

  /** Reads everything again, as on arrival, unless that would lose marks not saved yet. */
  refresh(): void {
    if (this.unsavedMarks()) return;
    this.load();
  }

  /** Whether the grade being marked has changes not saved yet, which a new list would discard; it says so. */
  private unsavedMarks(): boolean {
    if (!this.marks.dirty) return false;
    this.state.error.set('Save or close the grade you are marking first.');
    return true;
  }

  load(): void {
    const classroomId = this.classroomId();
    this.grades.clear();
    this.published.clear();
    this.stopMarking();
    this.names$ = null;
    if (!classroomId) return;
    const assessmentId = this.assessmentId();
    void this.grades.load((current) => this.read(classroomId, assessmentId, current));
    void this.published.load(async () =>
      (await this.assessmentApi.list(classroomId)).filter((a) => a.status === 'published'),
    );
  }

  /** Shows the grades of the assessment the filter names, or the newest of all, from their first page. */
  choose(): void {
    if (this.unsavedMarks()) {
      this.filter.setValue(this.assessmentId());
      return;
    }
    this.assessmentId.set(this.filter.value);
    this.page.set(0);
    this.showPage();
  }

  /** Shows the page the pager moved to. Marking a grade (which leaves the page) keeps the pager disabled while it
   * has unsaved changes. */
  showPage(): void {
    if (this.unsavedMarks()) {
      this.page.set(this.grades.value()?.page ?? 0);
      return;
    }
    this.stopMarking();
    const classroomId = this.classroomId();
    const assessmentId = this.assessmentId();
    if (classroomId) void this.grades.load((current) => this.read(classroomId, assessmentId, current));
  }

  /** The page of evaluations asked for with the names of their students, assessments, questions and topics. A page
   * past the end, after grades leave the list, falls back to the last one; `current` says whether the read is still
   * the one on show, so only then does the page asked for change. */
  private async read(classroomId: string, assessmentId: string, current: () => boolean): Promise<Grades> {
    let page = this.page();
    let result = await this.grading.evaluations(classroomId, assessmentId, page * PAGE_SIZE, PAGE_SIZE);
    if (!result.evaluations.length && result.total && page > 0) {
      page = Math.ceil(result.total / PAGE_SIZE) - 1;
      result = await this.grading.evaluations(classroomId, assessmentId, page * PAGE_SIZE, PAGE_SIZE);
    }
    const grades = result.evaluations;
    const [students, assessments, unreleased] = await Promise.all([
      this.studentNames(classroomId),
      assessmentsOf(grades, (id) => this.assessmentApi.get(id)),
      assessmentId ? this.grading.counts(classroomId, assessmentId).then((c) => c.unreleased) : 0,
      // Without the curriculum, topics show as ids.
      this.curriculum.need(grades.flatMap((g) => g.evidence.map((e) => e.topic_id))).catch(() => undefined),
    ]);
    if (current()) this.page.set(page);
    return { grades, page, total: result.total, unreleased, students, assessments };
  }

  /** The classroom's students' names, read once for it; a failed read is tried again next time. */
  private studentNames(classroomId: string): Promise<Map<string, string>> {
    if (this.names$?.classroomId !== classroomId) {
      const names = this.identity.roster(classroomId).then(studentNames);
      names.catch(() => {
        if (this.names$?.names === names) this.names$ = null;
      });
      this.names$ = { classroomId, names };
    }
    return this.names$.names;
  }

  /** Re-reads the page on show after a change that already succeeded. A failed re-read keeps what is on show and
   * says so on the page, beside the change's own success. */
  private async reread(classroomId: string, assessmentId: string): Promise<void> {
    const same = () => this.classroomId() === classroomId && this.assessmentId() === assessmentId;
    try {
      await this.grades.reload(() => this.read(classroomId, assessmentId, same));
    } catch (error) {
      if (same())
        this.state.error.set(
          `The grades could not be refreshed. ${messageOf(error, 'Choose Refresh grades to try again.')}`,
        );
    }
  }

  /** The student's roster name, or '' for a student no longer enrolled or not yet named. */
  studentName(grade: Evaluation): string {
    return this.grades.value()?.students.get(grade.student_id) ?? '';
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
    const criterion = this.names
      .question(grade, evidence)
      ?.rubric?.criteria.find((c) => c.id === criterionId);
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
      void this.grading.evaluation(grade.id).then(
        (fresh) => {
          const current = this.selected();
          if (current?.id !== grade.id || this.classroomId() !== classroomId) return;
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
        // The grade is final whether or not the list can be read again: say so either way.
        await this.reread(classroomId, assessmentId);
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
      // Released, so the page says so at once, after the dialog has closed; a failed re-read is reported beside it.
      this.state.error.set('');
      this.state.message.set(
        `${result.released} ${result.released === 1 ? 'result' : 'results'} released to students.`,
      );
      await this.reread(classroomId, assessmentId);
    });
  }

  assessmentTitle(id: string): string {
    return this.published.value()?.find((a) => a.id === id)?.title ?? 'this assessment';
  }
}
