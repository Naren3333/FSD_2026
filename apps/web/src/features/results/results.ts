import { Component, inject, signal } from '@angular/core';
import { AssessmentApi, CurriculumApi, GradingApi } from '../../api/clients';
import type { Assessment, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { ActionState } from '../../shared/action-state';
import { assessmentsOf, promptOf, skillNames, total } from '../../shared/names';
import { StatusComponent } from '../../shared/status';

/** A student's own finalized grades in the current classroom. Drafts stay with the teacher until finalized. */
@Component({
  selector: 'app-results',
  standalone: true,
  imports: [StatusComponent],
  templateUrl: './results.html',
})
export class ResultsComponent {
  private readonly grading = inject(GradingApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly results = signal<Evaluation[]>([]);
  private readonly assessments = signal(new Map<string, Assessment>());
  private readonly skills = signal(new Map<string, string>());
  readonly total = total;

  constructor() {
    onClassroomChange(() => {
      this.results.set([]);
      this.load();
    });
  }

  load(): void {
    const classroomId = this.classroomId();
    if (!classroomId) return;
    void this.state.run(async () => {
      const results = await this.grading.mine(classroomId);
      // The student's copy of each assessment: prompts and options, never the answer key.
      const [assessments, nodes] = await Promise.all([
        assessmentsOf(results, (id) => this.assessmentApi.get(id)),
        this.curriculum.nodes(),
      ]);
      this.assessments.set(assessments);
      this.skills.set(skillNames(nodes));
      this.results.set(results);
    });
  }

  title(result: Evaluation): string {
    return this.assessments().get(result.assessment_id)?.title ?? 'Assessment';
  }
  prompt(result: Evaluation, evidence: Evidence): string {
    return promptOf(this.assessments(), result.assessment_id, evidence.question_id);
  }
  skill(evidence: Evidence): string {
    return this.skills().get(evidence.skill_id) ?? '';
  }
  finalizedOn(result: Evaluation): string {
    return result.finalized_at
      ? new Date(result.finalized_at).toLocaleDateString('en-SG', { dateStyle: 'medium' })
      : '';
  }
}
