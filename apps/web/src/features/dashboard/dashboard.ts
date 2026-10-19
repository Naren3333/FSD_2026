import { PercentPipe } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AnalyticsApi, AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type {
  AssessmentProgress,
  ClassPerformance,
  GradingCounts,
  TopicMetric,
  Student,
} from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { studentNames } from '../../shared/names';
import { PagerComponent, pageCount } from '../../shared/pager';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [NoClassroomComponent, RouterLink, PercentPipe, SectionStateComponent, PagerComponent],
  templateUrl: './dashboard.html',
})
export class DashboardComponent {
  private readonly identity = inject(IdentityApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly grading = inject(GradingApi);
  private readonly analytics = inject(AnalyticsApi);
  private readonly curriculum = inject(CurriculumNames);
  private readonly context = inject(ClassroomContext);
  readonly classroomId = this.context.classroomId;
  readonly classroomName = this.context.classroomName;
  readonly roster = new Section<Student[]>();
  readonly assessments = new Section<AssessmentProgress[]>();
  readonly counts = new Section<GradingCounts>();
  readonly performance = new Section<ClassPerformance>();
  private readonly names = computed(() => studentNames(this.roster.value() ?? []));
  /** The class topics table shows this many students at a time. */
  readonly pageSize = 20;
  readonly enrolledTotal = signal<number | undefined>(undefined);
  readonly page = signal(0);
  /** The topics any student on this page has evidence for, sorted by name (or id, for a topic with none); `name` is
   * '' when the curriculum has none. */
  readonly topicRows = computed(() => {
    const ids = new Set(this.performance.value()?.students.flatMap((s) => s.topics.map((m) => m.topic_id)));
    return [...ids]
      .map((id) => ({ id, name: this.curriculum.topicName(id) }))
      .sort((a, b) => (a.name || a.id).localeCompare(b.name || b.id));
  });
  /** The summary tiles, each linking to where the teacher acts on it. The submissions panel below shows why the
   * assessments failed to load, so that tile repeats no error. */
  readonly stats = computed(() => {
    const c = this.counts.value();
    const unreleased = c?.unreleased ? ` · ${c.unreleased} not released` : '';
    return [
      tile(
        this.counts,
        'Needs grading',
        '/review',
        c?.drafts,
        `${c?.finalized} finalized${unreleased} · Open Grading review`,
      ),
      tile(
        this.roster,
        'Students enrolled',
        '/classrooms',
        this.enrolledTotal(),
        'Manage students under Classrooms',
      ),
      {
        ...tile(
          this.assessments,
          'Published assessments',
          '/assessments',
          this.assessments.value()?.length,
          'Published · drafts are under Assessments',
        ),
        error: '',
      },
    ];
  });
  /** Each student's evidence on this page, by topic. */
  private readonly metrics = computed(
    () =>
      new Map(
        this.performance
          .value()
          ?.students.map((s) => [s.student_id, new Map(s.topics.map((m) => [m.topic_id, m]))]),
      ),
  );

  constructor() {
    onClassroomChange((classroomId) => this.load(classroomId));
  }

  refresh(): void {
    this.load(this.classroomId(), this.page());
  }

  private load(classroomId: string, page = 0): void {
    for (const section of [this.roster, this.assessments, this.counts, this.performance]) section.clear();
    this.page.set(page);
    if (!classroomId) return;
    this.enrolledTotal.set(undefined);
    // Students never see drafts, so a draft has no submissions to count.
    void this.assessments.load(async () =>
      (await this.assessmentApi.progress(classroomId)).filter((a) => a.status === 'published'),
    );
    void this.counts.load(() => this.grading.counts(classroomId));
    void this.loadPerformance(classroomId);
  }

  /** Loads the class topics page on show; a page past the end, after students leave, falls back to the last one. */
  loadPerformance(classroomId = this.classroomId()): Promise<void> {
    this.loadRoster(classroomId, this.page());
    return this.performance.load(async (current) => {
      let page = await this.analytics.classPerformance(
        classroomId,
        this.page() * this.pageSize,
        this.pageSize,
      );
      if (!page.students.length && this.page() > 0) {
        const last = Math.max(0, pageCount(page.total, this.pageSize) - 1);
        if (page.total)
          page = await this.analytics.classPerformance(classroomId, last * this.pageSize, this.pageSize);
        if (current()) {
          this.page.set(last);
          this.loadRoster(classroomId, last);
        }
      }
      // Without the curriculum, topics show their ids.
      await this.curriculum
        .need(page.students.flatMap((s) => s.topics.map((m) => m.topic_id)))
        .catch(() => undefined);
      return page;
    });
  }

  private loadRoster(classroomId: string, page: number): void {
    void this.roster.load(async (current) => {
      const roster = await this.identity.rosterPage(classroomId, page * this.pageSize, this.pageSize);
      if (current()) this.enrolledTotal.set(roster.total);
      return roster.students;
    });
  }

  studentName(studentId: string): string {
    return this.names().get(studentId) ?? studentId;
  }

  metric(studentId: string, topicId: string): TopicMetric | undefined {
    return this.metrics().get(studentId)?.get(topicId);
  }

  /** "12 of 20 students submitted", or just the count when former students' submissions exceed the roster. */
  submitted(progress: AssessmentProgress): string {
    const enrolled = this.enrolledTotal();
    const noun = progress.submissions === 1 ? 'student' : 'students';
    return enrolled !== undefined && progress.submissions <= enrolled
      ? `${progress.submissions} of ${enrolled} students submitted`
      : `${progress.submissions} ${noun} submitted`;
  }
}

/** A summary tile: its count and caption once `section` has loaded, else whether it is loading or unavailable. */
function tile(
  section: Section<unknown>,
  label: string,
  link: string,
  count: number | undefined,
  caption: string,
) {
  const loading = section.loading();
  return {
    label,
    link,
    value: count !== undefined ? String(count) : loading ? '…' : '–',
    caption: count !== undefined ? caption : loading ? 'Loading…' : 'Unavailable',
    error: section.error(),
  };
}
