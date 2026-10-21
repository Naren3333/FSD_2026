import { PercentPipe } from '@angular/common';
import { Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AnalyticsApi, AssessmentApi, GradingApi } from '../../api/clients';
import type { AssessmentSummary, OwnResult, Performance } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { dueState } from '../../shared/due';
import { dateTime } from '../../shared/format';
import { MarksPipe } from '../../shared/marks';
import { EvidenceNames } from '../../shared/names';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';

/* a student's start page for the current classroom: what is still to do, what waits for the teacher, recent finalized results and the topics to practise. */
@Component({
  selector: 'app-home',
  standalone: true,
  imports: [NoClassroomComponent, RouterLink, PercentPipe, SectionStateComponent, MarksPipe],
  templateUrl: './home.html',
})
export class HomeComponent {
  private readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly grading = inject(GradingApi);
  private readonly analytics = inject(AnalyticsApi);
  private readonly curriculum = inject(CurriculumNames);
  private readonly context = inject(ClassroomContext);
  readonly classroomId = this.context.classroomId;
  readonly classroomName = this.context.classroomName;
  readonly assessments = new Section<AssessmentSummary[]>();
  readonly results = new Section<OwnResult[]>();
  readonly performance = new Section<Performance>();
  /* recent results shown here; the rest are under my results. */
  readonly recentCount = 3;

  /* assessments still to submit that still take submissions, soonest due first; those with no due date last. */
  readonly toDo = computed(() =>
    (this.assessments.value() ?? [])
      .filter((a) => !a.submitted && dueState(a.due_at, a.late_policy) !== 'closed')
      .sort((a, b) => (a.due_at ?? '9999').localeCompare(b.due_at ?? '9999')),
  );
  /* submitted assessments with no finalized result yet. */
  readonly waiting = computed(() => {
    const assessments = this.assessments.value();
    const results = this.results.value();
    if (!assessments || !results) return null;
    const graded = new Set(results.map((r) => r.assessment_id));
    return assessments.filter((a) => a.submitted && !graded.has(a.id)).length;
  });
  readonly recent = computed(() => this.results.value()?.slice(0, this.recentCount) ?? []);
  /* what the to-do list says when it is empty: whether there was nothing, everything was submitted, or some closed before the student submitted them. */
  readonly nothingToDo = computed(() => {
    const assessments = this.assessments.value() ?? [];
    if (!assessments.length) return 'No assessments in this classroom yet.';
    const missed = assessments.filter((a) => !a.submitted).length;
    if (!missed) return 'All caught up. You have submitted every assessment.';
    return `Nothing left to submit. ${missed} ${missed === 1 ? 'assessment' : 'assessments'} closed before you submitted.`;
  });
  readonly practise = computed(
    () => this.performance.value()?.topics.filter((s) => s.weakness_indicator) ?? [],
  );
  private readonly byId = computed(() => new Map((this.assessments.value() ?? []).map((a) => [a.id, a])));
  readonly names = new EvidenceNames(
    () => this.byId(),
    (id) => this.curriculum.topicName(id),
  );

  constructor() {
    onClassroomChange((classroomId) => this.load(classroomId));
  }

  refresh(): void {
    this.load(this.classroomId());
  }

  private load(classroomId: string): void {
    for (const section of [this.assessments, this.results, this.performance]) section.clear();
    const studentId = this.session.profile()?.id;
    if (!classroomId || !studentId) return;
    void this.assessments.load(() => this.assessmentApi.list(classroomId));
    void this.results.load(() => this.grading.mine(classroomId));
    void this.performance.load(async () => {
      const performance = await this.analytics.performance(studentId, classroomId);
      // without the curriculum, topics show their ids.
      await this.curriculum.need(performance.topics.map((s) => s.topic_id)).catch(() => undefined);
      return performance;
    });
  }

  /* "due 13 oct 2026, 3:00 pm", or "overdue since …" once past; '' without a due date. */
  due(item: AssessmentSummary): string {
    if (!item.due_at) return '';
    return this.overdue(item) ? `Overdue since ${dateTime(item.due_at)}` : `Due ${dateTime(item.due_at)}`;
  }
  overdue(item: AssessmentSummary): boolean {
    return dueState(item.due_at, item.late_policy) === 'late';
  }
}
