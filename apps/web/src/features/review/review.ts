import { Component, DestroyRef, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { NavigationEnd, Router } from '@angular/router';
import { filter } from 'rxjs';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, AssessmentSummary, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { onSectionShown, rereadQuietly, sectionOf } from '../../core/section-shown';
import { CurriculumNames } from '../../core/curriculum-names';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { messageOf } from '../../shared/errors';
import { dateTime } from '../../shared/format';
import { IconComponent } from '../../shared/icon';
import { MarksPipe } from '../../shared/marks';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { EvidenceNames, assessmentsOf, studentNames } from '../../shared/names';
import { PagerComponent } from '../../shared/pager';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import {
  applySuggestion,
  flagText,
  markingForm,
  marksOf,
  suggestionReason,
  type MarkingForm,
} from './marking';
import { focusLater } from '../../shared/focus';
import { readPage } from '../../shared/paged-section';

/* load evaluations, display names, totals and unreleased grade counts. */
interface Grades {
  grades: Evaluation[];
  /* the page these are, counted from 0. */
  page: number;
  total: number;
  unreleased: number;
  students: Map<string, string>;
  assessments: Map<string, Assessment>;
}

/* how many grades one page shows; each carries the student's answers. */
const PAGE_SIZE = 20;

/* how long an open grade first waits before re-reading ai suggestions still being prepared, and the longest wait. */
const SUGGESTION_POLL = 5000;
const SUGGESTION_POLL_MAX = 60000;

/* which of the page's grades the queue shows. */
export type GradeFilter = 'all' | 'mark' | 'draft' | 'finalized';

/* the first draft after the grade `id` in the list, wrapping to earlier ones; the first draft when `id` is not in it. */
export function draftAfter(list: readonly Evaluation[], id: string): Evaluation | null {
  const draft = (g: Evaluation) => g.status === 'draft' && g.id !== id;
  const at = list.findIndex((g) => g.id === id);
  if (at < 0) return list.find(draft) ?? null;
  return list.slice(at + 1).find(draft) ?? list.slice(0, at).find(draft) ?? null;
}

@Component({
  selector: 'app-review',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
    PagerComponent,
    NoClassroomComponent,
    MarksPipe,
    IconComponent,
  ],
  templateUrl: './review.html',
})
export class ReviewComponent {
  private readonly grading = inject(GradingApi);
  private readonly identity = inject(IdentityApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  private readonly router = inject(Router);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  /* releasing an assessment's results, in its confirmation dialog. */
  readonly releaseState = new ActionState();
  readonly grades = new Section<Grades>();
  readonly names = new EvidenceNames(
    () => this.grades.value()?.assessments,
    (id) => this.curriculum.topicName(id),
  );
  /* the classroom's published assessments, to choose one to mark. */
  readonly published = new Section<AssessmentSummary[]>();
  readonly filter = new FormControl('', { nonNullable: true });
  /* the assessment whose grades are shown; '' for the classroom's newest grades. */
  readonly assessmentId = signal('');
  /* the page of grades asked for, counted from 0. */
  readonly page = signal(0);
  readonly pageSize = PAGE_SIZE;
  /* the draft being marked. */
  readonly selected = signal<Evaluation | null>(null);
  /* a finalized grade shown read-only. */
  readonly viewing = signal<Evaluation | null>(null);
  /* the grade in the grade panel, being marked or read; a finalized one as the page last read it, so a release shows. */
  readonly open = computed(() => {
    const viewing = this.viewing();
    if (this.selected() || !viewing) return this.selected();
    return this.grades.value()?.grades.find((g) => g.id === viewing.id) ?? viewing;
  });
  /* the queue's status filter, applied to the loaded page only. */
  readonly gradeFilter = signal<GradeFilter>('all');
  readonly filters: readonly { id: GradeFilter; label: string; empty: string }[] = [
    { id: 'all', label: 'All', empty: 'No grades on this page.' },
    { id: 'mark', label: 'To mark', empty: 'No grades to mark on this page.' },
    { id: 'draft', label: 'Drafts', empty: 'No draft grades on this page.' },
    { id: 'finalized', label: 'Finalized', empty: 'No finalized grades on this page.' },
  ];
  /* how many of the page's grades each filter keeps. */
  readonly counts = computed(() => {
    const grades = this.grades.value()?.grades ?? [];
    const count = (kept: GradeFilter) => grades.filter((g) => this.matches(g, kept)).length;
    return { all: grades.length, mark: count('mark'), draft: count('draft'), finalized: count('finalized') };
  });
  /* the page's grades the filter keeps, in queue order. */
  readonly shown = computed(() => {
    const kept = this.gradeFilter();
    return (this.grades.value()?.grades ?? []).filter((g) => this.matches(g, kept));
  });
  /* the next draft on the page after the open grade, whatever the filter shows. */
  readonly nextDraft = computed(() => {
    const open = this.open();
    return open ? draftAfter(this.grades.value()?.grades ?? [], open.id) : null;
  });
  /* no other draft on this page, but a next page to look on. */
  readonly nextOnNextPage = computed(() => {
    const value = this.grades.value();
    return !this.nextDraft() && !!value && (value.page + 1) * PAGE_SIZE < value.total;
  });
  /* the row before the open grade in the queue as shown. */
  readonly previousGrade = computed(() => {
    const list = this.shown();
    const at = list.findIndex((g) => g.id === this.open()?.id);
    return at > 0 ? list[at - 1] : null;
  });
  /* correct automatic answers whose override controls are showing, by question id. */
  readonly overriding = signal<ReadonlySet<string>>(new Set());
  readonly finalizing = signal(false);
  readonly releasing = signal(false);
  marks: MarkingForm = new FormGroup({});
  @ViewChild('dialog') dialog?: ConfirmDialogComponent;
  @ViewChild('releaseDialog') releaseDialog?: ConfirmDialogComponent;
  readonly form = new FormGroup({
    reason: new FormControl('', { nonNullable: true, validators: requiredText(1000) }),
  });
  /* the chosen assessment's finalized grades its enrolled students cannot see yet, across every page: what a release releases. */
  readonly unreleased = computed(() => (this.assessmentId() ? (this.grades.value()?.unreleased ?? 0) : 0));
  readonly flagText = flagText;
  readonly suggestionReason = suggestionReason;
  private poll: ReturnType<typeof setTimeout> | undefined;
  /* the classroom's students' names, read once per classroom (or refresh) rather than for every page. */
  private names$: { classroomId: string; names: Promise<Map<string, string>> } | null = null;
  /* the grade the address asks for, to open once the page has loaded ('' closes the open one), or null. */
  private requestedGrade: string | null = null;
  /* next to mark moved to the next page, so its first draft opens once it loads. */
  private firstDraft = false;
  /* counts each grade opened or closed, so a grade read for the address lands only if nothing changed since. */
  private opening = 0;
  /* the section of the last address followed. */
  private previousSection = '';

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.marks.dirty);
    inject(DestroyRef).onDestroy(() => clearTimeout(this.poll));
    let shownClassroom = '';
    onClassroomChange((classroomId) => {
      // the assessment and grade of the classroom left behind no longer belong in the address.
      const changed = !!shownClassroom && classroomId !== shownClassroom;
      shownClassroom = classroomId;
      if (changed) {
        this.filter.setValue('');
        this.assessmentId.set('');
        this.requestedGrade = null;
      }
      this.page.set(0);
      this.gradeFilter.set('all');
      this.load();
      if (changed) this.putAddress(true);
    });
    this.router.events
      .pipe(
        filter((event): event is NavigationEnd => event instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe((event) => this.followAddress(event.urlAfterRedirects));
    // the current page of grades, unless one is open for marking.
    onSectionShown('review', () => {
      const classroomId = this.classroomId();
      const assessmentId = this.assessmentId();
      if (!classroomId || this.selected()) return;
      // a student enrolled since the names were read would show without one.
      this.names$ = null;
      const same = () => this.classroomId() === classroomId && this.assessmentId() === assessmentId;
      rereadQuietly(this.grades, () => this.read(classroomId, assessmentId, same));
    });
  }

  /* reads everything again, as on arrival, unless that would lose marks not saved yet. */
  refresh(): void {
    if (this.unsavedMarks()) return;
    this.load();
    this.putAddress(true);
  }

  /* whether the grade being marked has changes not saved yet, which a new list would discard; it says so. */
  private unsavedMarks(): boolean {
    if (!this.marks.dirty) return false;
    this.state.error.set('Save or close the grade you are marking first.');
    // the grade's status sits under its questions, out of view of the buttons that were refused.
    setTimeout(() =>
      document.querySelector('.grade-panel app-status')?.scrollIntoView?.({ block: 'nearest' }),
    );
    return true;
  }

  load(): void {
    const classroomId = this.classroomId();
    this.grades.clear();
    this.published.clear();
    this.stopMarking();
    this.names$ = null;
    this.firstDraft = false;
    if (!classroomId) return;
    this.loadGrades();
    void this.published
      .load(async () => (await this.assessmentApi.list(classroomId)).filter((a) => a.status === 'published'))
      .then(() => {
        if (this.classroomId() === classroomId) this.checkAssessment();
      });
  }

  /* reads the page asked for, then opens any grade waiting for it. */
  private loadGrades(): void {
    const classroomId = this.classroomId();
    const assessmentId = this.assessmentId();
    if (!classroomId) return;
    void this.grades
      .load((current) => this.read(classroomId, assessmentId, current))
      .then(() => this.openRequested());
  }

  /* shows the grades of the assessment the filter names, or the newest of all, from their first page. */
  choose(): void {
    if (this.unsavedMarks()) {
      this.filter.setValue(this.assessmentId());
      return;
    }
    this.assessmentId.set(this.filter.value);
    this.showFirstPage();
    this.putAddress();
  }

  /* the first page of the grades chosen, with nothing open and every grade shown. */
  private showFirstPage(): void {
    this.firstDraft = false;
    this.page.set(0);
    this.gradeFilter.set('all');
    this.stopMarking();
    this.loadGrades();
  }

  /* shows the page the pager moved to. */
  showPage(): void {
    if (this.unsavedMarks()) {
      this.page.set(this.grades.value()?.page ?? 0);
      return;
    }
    this.firstDraft = false;
    this.gradeFilter.set('all');
    this.stopMarking();
    this.loadGrades();
    this.putAddress(true);
  }

  /* an assessment the address names that is not one of the classroom's published ones falls back to the newest grades. */
  private checkAssessment(): void {
    const id = this.assessmentId();
    const list = this.published.value();
    if (!id || !list || list.some((a) => a.id === id)) return;
    this.state.error.set('That assessment is not in this classroom.');
    // a grade asked for with it cannot be in this classroom either.
    this.requestedGrade = null;
    this.filter.setValue('');
    this.assessmentId.set('');
    this.showFirstPage();
    this.putAddress(true);
  }

  /* the page of evaluations asked for with the names of their students, assessments, questions and topics. */
  private async read(classroomId: string, assessmentId: string, current: () => boolean): Promise<Grades> {
    const { result, page } = await readPage(
      this.page(),
      PAGE_SIZE,
      (offset, limit) => this.grading.evaluations(classroomId, assessmentId, offset, limit),
      (r) => !r.evaluations.length,
    );
    const grades = result.evaluations;
    const [students, assessments, unreleased] = await Promise.all([
      this.studentNames(classroomId),
      assessmentsOf(grades, (id) => this.assessmentApi.get(id)),
      assessmentId ? this.grading.counts(classroomId, assessmentId).then((c) => c.unreleased) : 0,
      // without the curriculum, topics show as ids.
      this.curriculum.need(grades.flatMap((g) => g.evidence.map((e) => e.topic_id))).catch(() => undefined),
    ]);
    if (current()) this.page.set(page);
    return { grades, page, total: result.total, unreleased, students, assessments };
  }

  /* the classroom's students' names, read once for it; a failed read is tried again next time. */
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

  /* re-reads the page on show after a change that already succeeded. */
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

  // --- the address
  // ---------------------------------------------------------------------------------------------

  /* puts the chosen assessment and the open grade in the address, while the review section is on show. */
  private putAddress(replaceUrl = false): void {
    if (sectionOf(this.router.url) !== 'review') return;
    const params = this.router.parseUrl(this.router.url).queryParamMap;
    const assessment = this.assessmentId();
    const grade = this.open()?.id ?? '';
    if ((params.get('assessment') ?? '') === assessment && (params.get('grade') ?? '') === grade) return;
    void this.router.navigate(['/review'], {
      queryParams: { assessment: assessment || null, grade: grade || null },
      replaceUrl,
    });
  }

  /* follows an address in the review section: links, back and forward choose the assessment and grade. */
  private followAddress(url: string): void {
    const section = sectionOf(url);
    const from = this.previousSection;
    this.previousSection = section;
    if (section !== 'review') return;
    const params = this.router.parseUrl(url).queryParamMap;
    const assessment = params.get('assessment') ?? '';
    const grade = params.get('grade') ?? '';
    // the sidebar's bare link back to the kept-alive page keeps what is open.
    if (from !== 'review' && !assessment && !grade) {
      this.putAddress(true);
      return;
    }
    if (assessment !== this.assessmentId()) {
      if (this.unsavedMarks()) {
        this.putAddress(true);
        return;
      }
      this.requestedGrade = grade;
      this.filter.setValue(assessment);
      this.assessmentId.set(assessment);
      this.showFirstPage();
      this.checkAssessment();
      return;
    }
    this.requestedGrade = grade;
    this.openRequested();
  }

  /* once the page has loaded: opens next to mark's first draft, or the grade the address asks for (or closes it). */
  private openRequested(): void {
    if (!this.classroomId() || !this.grades.ready() || !this.grades.value()) return;
    if (this.firstDraft) {
      this.firstDraft = false;
      this.requestedGrade = null;
      if (this.open()) return;
      const first = this.grades.value()?.grades.find((g) => g.status === 'draft');
      if (first) {
        this.openGrade(first);
        this.putAddress();
      } else focusLater('grade-queue-heading');
      return;
    }
    const id = this.requestedGrade;
    if (id === null) return;
    this.requestedGrade = null;
    if ((this.open()?.id ?? '') === id) return;
    if (this.unsavedMarks()) {
      this.putAddress(true);
      return;
    }
    if (!id) {
      this.closeGrade(true);
      return;
    }
    const onPage = this.grades.value()?.grades.find((g) => g.id === id);
    if (onPage) {
      this.openGrade(onPage);
      return;
    }
    // a link can name a grade on another page; it must belong to the classroom and assessment shown.
    const classroomId = this.classroomId();
    const assessmentId = this.assessmentId();
    const token = ++this.opening;
    const current = () =>
      token === this.opening && this.classroomId() === classroomId && this.assessmentId() === assessmentId;
    void this.readGrade(id, classroomId, assessmentId).then(
      (grade) => {
        if (current()) this.openGrade(grade);
      },
      () => {
        if (!current()) return;
        this.state.error.set('The grade could not be opened.');
        this.putAddress(true);
      },
    );
  }

  /* a grade not on the page, with its assessment's questions and its topics' names, so it can be marked. */
  private async readGrade(id: string, classroomId: string, assessmentId: string): Promise<Evaluation> {
    const grade = await this.grading.evaluation(id);
    if (grade.classroom_id !== classroomId || (assessmentId && grade.assessment_id !== assessmentId))
      throw new Error('The grade could not be opened.');
    if (!this.grades.value()?.assessments.has(grade.assessment_id)) {
      const assessment = await this.assessmentApi.get(grade.assessment_id);
      const value = this.grades.value();
      if (value)
        this.grades.value.set({
          ...value,
          assessments: new Map(value.assessments).set(grade.assessment_id, assessment),
        });
    }
    await this.curriculum.need(grade.evidence.map((e) => e.topic_id)).catch(() => undefined);
    return grade;
  }

  // --- the queue
  // ---------------------------------------------------------------------------------------------

  /* the student's roster name, or '' for a student no longer enrolled or not yet named. */
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
  private matches(grade: Evaluation, kept: GradeFilter): boolean {
    if (kept === 'mark') return grade.status === 'draft' && this.toMark(grade) > 0;
    if (kept === 'draft') return grade.status === 'draft';
    if (kept === 'finalized') return grade.status === 'finalized';
    return true;
  }
  filterEmpty(): string {
    return this.filters.find((f) => f.id === this.gradeFilter())?.empty ?? '';
  }

  // --- the grade panel
  // ---------------------------------------------------------------------------------------------

  /* opens a grade by a button (a draft for marking, a finalized one read-only), unless that would lose marks not saved yet. */
  show(grade: Evaluation): void {
    if (this.open()?.id === grade.id) {
      this.focusHeading(grade);
      return;
    }
    if (this.unsavedMarks()) return;
    this.state.error.set('');
    this.openGrade(grade);
    this.putAddress();
  }

  previous(): void {
    const grade = this.previousGrade();
    if (grade) this.show(grade);
  }

  nextToMark(): void {
    const next = this.nextDraft();
    if (next) {
      this.show(next);
      return;
    }
    if (!this.nextOnNextPage() || this.unsavedMarks()) return;
    this.page.set((this.grades.value()?.page ?? 0) + 1);
    this.showPage();
    this.firstDraft = true;
  }

  /* puts a grade in the panel: a draft in the marking form, a finalized grade read-only. */
  private openGrade(grade: Evaluation): void {
    this.stopMarking();
    this.opening++;
    if (grade.status === 'draft') {
      const questions = this.grades.value()?.assessments.get(grade.assessment_id)?.questions ?? [];
      this.marks = markingForm(grade.evidence, questions);
      this.selected.set(grade);
      this.watchSuggestions();
    } else {
      this.viewing.set(grade);
    }
    this.focusHeading(grade);
  }

  private focusHeading(grade: Evaluation): void {
    focusLater(`marking-${grade.id}`);
  }

  /* closes the open grade, discarding marks not saved yet; `focusRow` gives focus back to its row's button. */
  private closeGrade(focusRow: boolean): void {
    const id = this.open()?.id;
    this.stopMarking();
    this.opening++;
    if (focusRow && id) focusLater(`open-${id}`);
  }

  /* the panel's close, the explicit way out: marks not saved yet are discarded. */
  closePanel(): void {
    this.closeGrade(true);
    this.putAddress();
  }

  /* back to the queue on a narrow screen reads as navigation, so marks not saved yet hold it. */
  backToQueue(): void {
    if (this.unsavedMarks()) return;
    this.closePanel();
  }

  stopMarking(): void {
    clearTimeout(this.poll);
    this.marks = new FormGroup({});
    this.selected.set(null);
    this.viewing.set(null);
    this.overriding.set(new Set());
  }

  /* the open grade's student, or the fallback for one not on the roster. */
  studentLabel(grade: Evaluation): string {
    return this.studentName(grade) || 'Student not on the roster';
  }

  /* a correct answer marked automatically, shown as one line until the teacher overrides it. */
  collapsible(evidence: Evidence): boolean {
    return !evidence.needs_marking && evidence.marked_by === 'auto' && evidence.correct;
  }

  toggleOverride(questionId: string): void {
    const next = new Set(this.overriding());
    if (next.has(questionId)) next.delete(questionId);
    else next.add(questionId);
    this.overriding.set(next);
  }

  /* the marks the form gives so far, and how many questions still need marking. */
  markingSummary(grade: Evaluation): string {
    let awarded = 0;
    let possible = 0;
    let left = 0;
    for (const e of grade.evidence) {
      possible += e.possible;
      const value = this.marks.controls[e.question_id]?.getRawValue();
      const levels = Object.values(value?.criteria ?? {});
      let points: number | null = value?.awarded ?? null;
      if (levels.length)
        points = levels.every((p) => p !== null)
          ? levels.reduce<number>((sum, p) => sum + Number(p), 0)
          : null;
      if (points !== null) awarded += Number(points);
      else if (e.needs_marking) left++;
      else awarded += e.awarded;
    }
    const rest = left
      ? `${left} ${left === 1 ? 'question' : 'questions'} left to mark`
      : 'every question marked';
    return `${awarded} of ${possible} marks · ${rest}`;
  }

  // --- marking
  // ---------------------------------------------------------------------------------------------

  useSuggestion(evidence: Evidence): void {
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

  /* puts a re-read grade in the list and, when it is the one being marked, in the marking form. */
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

  /* while a suggestion is being prepared, re-reads the grade so it appears without a refresh, less often the longer it takes. */
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
          // only the suggestions change here, so the teacher's unsaved marks stay.
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
    const grade = this.selected();
    if (!grade) return;
    // an override left invalid and collapsed again is shown, so its error can take focus.
    const invalid = grade.evidence.filter(
      (e) => this.collapsible(e) && this.marks.controls[e.question_id]?.invalid,
    );
    if (invalid.length)
      this.overriding.set(new Set([...this.overriding(), ...invalid.map((e) => e.question_id)]));
    if (!this.state.validate(this.marks, 'marking-form')) return;
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
    let finalized = false;
    void this.state
      .run(
        async () => {
          const result = await this.grading.finalize(grade.id, this.form.controls.reason.value, marks);
          finalized = true;
          this.close();
          this.stopMarking();
          // the queue shows it finalized even if the page cannot be read again.
          this.replace(result);
          // the grade is final whether or not the list can be read again: say so either way.
          await this.reread(classroomId, assessmentId);
        },
        grade.release_policy === 'manual'
          ? 'Grade finalized. Students see it when you release the results.'
          : 'Grade finalized. Learning performance will update in a moment.',
      )
      .then(() => {
        if (finalized && this.classroomId() === classroomId && this.assessmentId() === assessmentId)
          this.openNextAfter(grade.id);
      });
  }

  /* after finalizing, the next draft to mark opens with its heading, naming the student, focused; with none left, the queue's heading is. */
  private openNextAfter(id: string): void {
    if (this.open()) return;
    const next = draftAfter(this.grades.value()?.grades ?? [], id);
    if (next) this.openGrade(next);
    else focusLater('grade-queue-heading');
    this.putAddress(true);
  }

  // --- releasing
  // -------------------------------------------------------------------------------------------

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
      // released, so the page says so at once, after the dialog has closed; a failed re-read is reported
      // beside it.
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
