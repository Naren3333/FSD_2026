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
import { PagerComponent } from '../../shared/pager';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [RouterLink, PercentPipe, SectionStateComponent, PagerComponent],
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
  readonly page = signal(0);
  /** The topics any student on this page has evidence for, by name (or id, for a topic with none). */
  readonly topicRows = computed(() => {
    const ids = new Set(this.performance.value()?.students.flatMap((s) => s.topics.map((m) => m.topic_id)));
    const label = (id: string) => this.topic(id) || id;
    return [...ids].sort((a, b) => label(a).localeCompare(label(b)));
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
        this.roster.value()?.length,
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
    void this.roster.load(() => this.identity.roster(classroomId));
    // Students never see drafts, so a draft has no submissions to count.
    void this.assessments.load(async () =>
      (await this.assessmentApi.progress(classroomId)).filter((a) => a.status === 'published'),
    );
    void this.counts.load(() => this.grading.counts(classroomId));
    void this.loadPerformance(classroomId);
  }

  /** Loads the class topics page on show. */
  loadPerformance(classroomId = this.classroomId()): Promise<void> {
    return this.performance.load(async () => {
      const page = await this.analytics.classPerformance(
        classroomId,
        this.page() * this.pageSize,
        this.pageSize,
      );
      await this.curriculum.need(page.students.flatMap((s) => s.topics.map((m) => m.topic_id)));
      return page;
    });
  }

  studentName(studentId: string): string {
    return this.names().get(studentId) ?? studentId;
  }

  /** The topic's name, or '' when the curriculum has none. */
  topic(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  metric(studentId: string, topicId: string): TopicMetric | undefined {
    return this.metrics().get(studentId)?.get(topicId);
  }

  /** "12 of 20 students submitted", or just the count when former students' submissions exceed the roster. */
  submitted(progress: AssessmentProgress): string {
    const enrolled = this.roster.value()?.length;
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
