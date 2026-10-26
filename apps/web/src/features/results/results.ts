import { Component, inject, signal } from '@angular/core';
import { AssessmentApi, GradingApi } from '../../api/clients';
import type { Assessment, Evidence, OwnResult } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { CurriculumNames } from '../../core/curriculum-names';
import { date } from '../../shared/format';
import { MarksPipe } from '../../shared/marks';
import { EvidenceNames, assessmentsOf } from '../../shared/names';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { Section } from '../../shared/section';
import { readPage } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';

/* a student's finalized results with the assessments they belong to. */
interface Results {
  results: OwnResult[];
  assessments: Map<string, Assessment>;
}

/* a student's own released grades in the current classroom. */
@Component({
  selector: 'app-results',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    NoClassroomComponent,
    SectionStateComponent,
    MarksPipe,
    PagerComponent,
  ],
  templateUrl: './results.html',
})
export class ResultsComponent {
  private readonly grading = inject(GradingApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly results = new Section<Results>();
  /* the page of results on show, counted from 0, and how many released results there are. */
  readonly page = signal(0);
  readonly total = signal(0);
  readonly pageSize = 20;
  readonly names = new EvidenceNames(
    () => this.results.value()?.assessments,
    (id) => this.curriculum.topicName(id),
  );

  constructor() {
    onClassroomChange(() => {
      this.page.set(0);
      this.load();
    });
    onSectionShown('results', () => {
      const classroomId = this.classroomId();
      if (classroomId) rereadQuietly(this.results, () => this.read(classroomId));
    });
  }

  load(): void {
    const classroomId = this.classroomId();
    this.results.clear();
    if (!classroomId) return;
    void this.results.load(() => this.read(classroomId));
  }

  private async read(classroomId: string): Promise<Results> {
    const { result, page } = await readPage(
      this.page(),
      this.pageSize,
      (offset, limit) => this.grading.minePage(classroomId, offset, limit),
      (r) => !r.items.length,
    );
    const results = result.items;
    this.page.set(page);
    this.total.set(result.total);
    // the student's copy of each assessment: prompts and options, never the answer key.
    const [assessments] = await Promise.all([
      assessmentsOf(results, (id) => this.assessmentApi.get(id)),
      // without the curriculum, topics show their ids.
      this.curriculum.need(results.flatMap((r) => r.evidence.map((e) => e.topic_id))).catch(() => undefined),
    ]);
    return { results, assessments };
  }

  outcome(evidence: Pick<Evidence, 'correct' | 'awarded'>): string {
    if (evidence.correct) return 'Correct';
    return evidence.awarded > 0 ? 'Part marks' : 'Incorrect';
  }
  /* the outcome chip's colour. */
  outcomeTone(evidence: Pick<Evidence, 'correct' | 'awarded'>): string {
    if (evidence.correct) return 'badge-good';
    return evidence.awarded > 0 ? 'badge-warning' : 'badge-danger';
  }
  finalizedOn(result: OwnResult): string {
    return result.finalized_at ? date(result.finalized_at) : '';
  }
}
