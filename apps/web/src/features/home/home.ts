import { PercentPipe } from '@angular/common';
import { Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AnalyticsApi, AssessmentApi, CurriculumApi, GradingApi } from '../../api/clients';
import type { AssessmentSummary, Evaluation, Performance } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { skillNames, total } from '../../shared/names';
import { Section } from '../../shared/section';

/** A student's start page for the current classroom: what is still to do, what waits for the teacher, recent
 * finalized results and the skills to practise. Grades show only once the teacher has finalized them. */
@Component({
  selector: 'app-home',
  standalone: true,
  imports: [RouterLink, PercentPipe],
  templateUrl: './home.html',
})
export class HomeComponent {
  private readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly grading = inject(GradingApi);
  private readonly analytics = inject(AnalyticsApi);
  private readonly curriculum = inject(CurriculumApi);
  private readonly context = inject(ClassroomContext);
  readonly classroomId = this.context.classroomId;
  readonly classroomName = computed(
    () => this.context.classrooms().find((room) => room.id === this.classroomId())?.name ?? '',
  );
  readonly assessments = new Section<AssessmentSummary[]>();
  readonly results = new Section<Evaluation[]>();
  readonly performance = new Section<Performance>();
  private readonly skills = signal(new Map<string, string>());
  readonly total = total;
  /** Recent results shown here; the rest are under My results. */
  readonly recentCount = 3;

  readonly toDo = computed(() => this.assessments.value()?.filter((a) => !a.submitted) ?? []);
  /** Submitted assessments with no finalized result yet. Needs both lists, so it waits for both. */
  readonly waiting = computed(() => {
    const assessments = this.assessments.value();
    const results = this.results.value();
    if (!assessments || !results) return null;
    const graded = new Set(results.map((r) => r.assessment_id));
    return assessments.filter((a) => a.submitted && !graded.has(a.id)).length;
  });
  readonly recent = computed(() => this.results.value()?.slice(0, this.recentCount) ?? []);
  readonly practise = computed(
    () => this.performance.value()?.skills.filter((s) => s.weakness_indicator) ?? [],
  );
  private readonly titles = computed(
    () => new Map((this.assessments.value() ?? []).map((a) => [a.id, a.title])),
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
      const [performance, nodes] = await Promise.all([
        this.analytics.performance(studentId, classroomId),
        this.curriculum.nodes(),
      ]);
      this.skills.set(skillNames(nodes));
      return performance;
    });
  }

  title(result: Evaluation): string {
    return this.titles().get(result.assessment_id) ?? 'Assessment';
  }

  skill(skillId: string): string {
    return this.skills().get(skillId) ?? skillId;
  }
}
