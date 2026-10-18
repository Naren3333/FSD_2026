import { Component, inject } from '@angular/core';
import { AssessmentApi, GradingApi } from '../../api/clients';
import type { Assessment, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { date } from '../../shared/format';
import { MarksPipe } from '../../shared/marks';
import { EvidenceNames, assessmentsOf } from '../../shared/names';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';

/** A student's finalized results with the assessments they belong to. */
interface Results {
  results: Evaluation[];
  assessments: Map<string, Assessment>;
}

/** A student's own released grades in the current classroom. Drafts stay with the teacher until finalized, and some
 * assessments' grades until the teacher releases them. */
@Component({
  selector: 'app-results',
  standalone: true,
  imports: [SectionStateComponent, MarksPipe],
  templateUrl: './results.html',
})
export class ResultsComponent {
  private readonly grading = inject(GradingApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly results = new Section<Results>();
  readonly names = new EvidenceNames(
    () => this.results.value()?.assessments,
    (id) => this.curriculum.topicName(id),
  );

  constructor() {
    onClassroomChange(() => this.load());
  }

  load(): void {
    const classroomId = this.classroomId();
    this.results.clear();
    if (!classroomId) return;
    void this.results.load(async () => {
      const results = await this.grading.mine(classroomId);
      // The student's copy of each assessment: prompts and options, never the answer key.
      const [assessments] = await Promise.all([
        assessmentsOf(results, (id) => this.assessmentApi.get(id)),
        this.curriculum.need(results.flatMap((r) => r.evidence.map((e) => e.topic_id))),
      ]);
      return { results, assessments };
    });
  }

  outcome(evidence: Evidence): string {
    if (evidence.correct) return 'Correct';
    return evidence.awarded > 0 ? 'Part marks' : 'Incorrect';
  }
  finalizedOn(result: Evaluation): string {
    return result.finalized_at ? date(result.finalized_at) : '';
  }
}
