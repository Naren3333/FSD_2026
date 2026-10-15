import { PercentPipe } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AnalyticsApi, AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type {
  AssessmentProgress,
  ClassPerformance,
  GradingCounts,
  SkillMetric,
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
  /** The class skills table shows this many students at a time. */
  readonly pageSize = 20;
  readonly page = signal(0);
  /** The skills any student on this page has evidence for, by name (or id, for a skill with none). */
  readonly skillRows = computed(() => {
    const ids = new Set(this.performance.value()?.students.flatMap((s) => s.skills.map((m) => m.skill_id)));
    const label = (id: string) => this.skill(id) || id;
    return [...ids].sort((a, b) => label(a).localeCompare(label(b)));
  });
  /** Each student's evidence on this page, by skill. */
  private readonly metrics = computed(
    () =>
      new Map(
        this.performance
          .value()
          ?.students.map((s) => [s.student_id, new Map(s.skills.map((m) => [m.skill_id, m]))]),
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
    void this.assessments.load(() => this.assessmentApi.progress(classroomId));
    void this.counts.load(() => this.grading.counts(classroomId));
    void this.loadPerformance(classroomId);
  }

  goTo(page: number): void {
    this.page.set(Math.max(0, page));
    void this.loadPerformance(this.classroomId());
  }

  private loadPerformance(classroomId: string): Promise<void> {
    return this.performance.load(async () => {
      const page = await this.analytics.classPerformance(
        classroomId,
        this.page() * this.pageSize,
        this.pageSize,
      );
      await this.curriculum.need(page.students.flatMap((s) => s.skills.map((m) => m.skill_id)));
      return page;
    });
  }

  studentName(studentId: string): string {
    return this.names().get(studentId) ?? studentId;
  }

  /** The skill's name, or '' when the curriculum has none. */
  skill(skillId: string): string {
    return this.curriculum.skillName(skillId);
  }

  metric(studentId: string, skillId: string): SkillMetric | undefined {
    return this.metrics().get(studentId)?.get(skillId);
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
