import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { NavigationEnd, Router } from '@angular/router';
import { filter } from 'rxjs';
import { AssessmentApi, GradingApi } from '../../api/clients';
import type { AssessmentSummary, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { onSectionShown, rereadQuietly, sectionOf } from '../../core/section-shown';
import { CurriculumNames } from '../../core/curriculum-names';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { messageOf } from '../../shared/errors';
import { IconComponent } from '../../shared/icon';
import { MarksPipe } from '../../shared/marks';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { EvidenceNames } from '../../shared/names';
import { PagerComponent } from '../../shared/pager';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import {
  applySuggestion,
  collapsible,
  flagText,
  levelName,
  markingForm,
  markingSummary,
  marksOf,
  outcome,
  submittedAt,
  suggestionReason,
  toMark,
  type MarkingForm,
} from './marking';
import { AssessmentActionsComponent, type AssessmentActionDone } from './assessment-actions';
import { ReopenComponent, type Reopened } from './reopen';
import { focusLater } from '../../shared/focus';
import {
  GRADE_FILTERS,
  GradeReader,
  PAGE_SIZE,
  draftAfter,
  matches,
  type GradeFilter,
  type Grades,
} from './grades';
import { SuggestionPoll } from './suggestions';

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
    AssessmentActionsComponent,
    ReopenComponent,
  ],
  templateUrl: './review.html',
})
export class ReviewComponent {
  private readonly grading = inject(GradingApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  private readonly router = inject(Router);
  private readonly reader = new GradeReader();
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
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
  readonly filters = GRADE_FILTERS;
  /* how many of the page's grades each filter keeps. */
  readonly counts = computed(() => {
    const grades = this.grades.value()?.grades ?? [];
    const count = (kept: GradeFilter) => grades.filter((g) => matches(g, kept)).length;
    return { all: grades.length, mark: count('mark'), draft: count('draft'), finalized: count('finalized') };
  });
  /* the page's grades the filter keeps, in queue order. */
  readonly shown = computed(() => {
    const kept = this.gradeFilter();
    return (this.grades.value()?.grades ?? []).filter((g) => matches(g, kept));
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
  marks: MarkingForm = new FormGroup({});
  @ViewChild('dialog') dialog?: ConfirmDialogComponent;
  /* the release and finalize-all banners, and the open finalized grade's reopen, each with its own dialog. */
  readonly actions = signal<AssessmentActionsComponent | undefined>(undefined);
  readonly reopener = signal<ReopenComponent | undefined>(undefined);
  @ViewChild(AssessmentActionsComponent) private set actionsQuery(
    child: AssessmentActionsComponent | undefined,
  ) {
    this.actions.set(child);
  }
  @ViewChild(ReopenComponent) private set reopenerQuery(child: ReopenComponent | undefined) {
    this.reopener.set(child);
  }
  readonly form = new FormGroup({
    reason: new FormControl('', { nonNullable: true, validators: requiredText(1000) }),
  });
  /* the chosen assessment's finalized grades its enrolled students cannot see yet, across every page: what a release releases. */
  readonly unreleased = computed(() => (this.assessmentId() ? (this.grades.value()?.unreleased ?? 0) : 0));
  /* the chosen assessment's drafts with nothing left to mark, across every page. */
  readonly ready = computed(() => (this.assessmentId() ? (this.grades.value()?.ready ?? 0) : 0));
  /* whether any of this page's dialogs is open, so the page's own status stays out of their way. */
  readonly dialogOpen = computed(
    () =>
      this.finalizing() ||
      !!this.actions()?.releasing() ||
      !!this.actions()?.bulkFinalizing() ||
      !!this.reopener()?.reopening(),
  );
  /* the chosen assessment's title, for the release and finalize-all dialogs. */
  readonly assessmentTitle = computed(
    () => this.published.value()?.find((a) => a.id === this.assessmentId())?.title,
  );
  /* the chosen assessment's release policy. */
  readonly releasePolicy = computed(
    () => this.grades.value()?.assessments.get(this.assessmentId())?.release_policy,
  );
  /* whether marks not saved yet hold an action on the whole assessment. */
  readonly blocked = () => this.unsavedMarks();
  readonly flagText = flagText;
  readonly suggestionReason = suggestionReason;
  readonly toMark = toMark;
  readonly outcome = outcome;
  readonly submittedAt = submittedAt;
  readonly collapsible = collapsible;
  private readonly suggestions = new SuggestionPoll(this.selected, this.classroomId);
  /* the grade the address asks for, to open once the page has loaded ('' closes the open one), or null. */
  private requestedGrade: string | null = null;
  /* next to mark moved to the next page, so its first draft opens once it loads. */
  private firstDraft = false;
  /* counts each grade opened or closed, so a grade read for the address lands only if nothing changed since. */
  private opening = 0;
  /* the section of the last address followed. */
  private previousSection = '';

  constructor() {
    inject(UnsavedChanges).track(
      () => this.form.dirty || !!this.reopener()?.reopenForm.dirty || this.marks.dirty,
    );
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
      this.reader.forget();
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
    this.reader.forget();
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

  /* the page of evaluations asked for, and the page it turned out to be while it is still wanted. */
  private async read(classroomId: string, assessmentId: string, current: () => boolean): Promise<Grades> {
    const value = await this.reader.read(classroomId, assessmentId, this.page());
    if (current()) this.page.set(value.page);
    return value;
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
    void this.reader.grade(id, classroomId, assessmentId, this.grades).then(
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

  // --- the queue
  // ---------------------------------------------------------------------------------------------

  /* the student's roster name, or '' for a student no longer enrolled or not yet named. */
  studentName(grade: Evaluation): string {
    return this.grades.value()?.students.get(grade.student_id) ?? '';
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
      this.suggestions.watch();
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
    this.suggestions.stop();
    this.marks = new FormGroup({});
    this.selected.set(null);
    this.viewing.set(null);
    this.overriding.set(new Set());
  }

  /* the open grade's student, or the fallback for one not on the roster. */
  studentLabel(grade: Evaluation): string {
    return this.studentName(grade) || 'Student not on the roster';
  }

  toggleOverride(questionId: string): void {
    const next = new Set(this.overriding());
    if (next.has(questionId)) next.delete(questionId);
    else next.add(questionId);
    this.overriding.set(next);
  }

  markingSummary(grade: Evaluation): string {
    return markingSummary(grade, this.marks);
  }

  // --- marking
  // ---------------------------------------------------------------------------------------------

  useSuggestion(evidence: Evidence): void {
    const marks = this.marks.controls[evidence.question_id];
    if (marks && evidence.suggestion?.status === 'ready') applySuggestion(marks, evidence.suggestion);
  }

  levelName(grade: Evaluation, evidence: Evidence, criterionId: string, points: number): string {
    return levelName(this.names.question(grade, evidence), criterionId, points);
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

  // --- the assessment's actions and reopening
  // -------------------------------------------------------------------------------------------

  /* a release or finalize-all succeeded: the page says so at once, after its dialog has closed; a failed re-read is
     reported beside it. */
  actionDone(done: AssessmentActionDone): void {
    this.state.error.set('');
    this.state.message.set(done.message);
    void this.reread(done.classroomId, done.assessmentId);
  }

  /* a draft open for marking may be final now; its marks were all saved, so nothing is lost. */
  finalizedAll(): void {
    if (this.selected()) this.closeGrade(false);
    this.putAddress(true);
  }

  /* opens the reopened grade for marking, while the page still shows it. */
  reopened({ result, classroomId, assessmentId }: Reopened): void {
    this.replace(result);
    if (this.classroomId() !== classroomId || this.assessmentId() !== assessmentId) return;
    if (this.viewing()?.id === result.id) this.openGrade(result);
    this.state.error.set('');
    this.state.message.set(
      'Grade reopened as a draft. The student no longer sees it until you finalize it again.',
    );
    // a reopened grade may have been one of the assessment's unreleased ones.
    void this.reread(classroomId, assessmentId);
  }
}
