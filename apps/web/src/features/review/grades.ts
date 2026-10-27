import { inject } from '@angular/core';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, Evaluation } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { assessmentsOf, studentNames } from '../../shared/names';
import { readPage } from '../../shared/paged-section';
import type { Section } from '../../shared/section';
import { toMark } from './marking';

/* load evaluations, display names, totals and unreleased grade counts. */
export interface Grades {
  grades: Evaluation[];
  /* the page these are, counted from 0. */
  page: number;
  total: number;
  unreleased: number;
  /* the chosen assessment's drafts with nothing left to mark, across every page: what finalizing all finalizes. */
  ready: number;
  students: Map<string, string>;
  assessments: Map<string, Assessment>;
}

/* how many grades one page shows; each carries the student's answers. */
export const PAGE_SIZE = 20;

/* which of the page's grades the queue shows. */
export type GradeFilter = 'all' | 'mark' | 'draft' | 'finalized';

export const GRADE_FILTERS: readonly { id: GradeFilter; label: string; empty: string }[] = [
  { id: 'all', label: 'All', empty: 'No grades on this page.' },
  { id: 'mark', label: 'To mark', empty: 'No grades to mark on this page.' },
  { id: 'draft', label: 'Drafts', empty: 'No draft grades on this page.' },
  { id: 'finalized', label: 'Finalized', empty: 'No finalized grades on this page.' },
];

/* whether the filter `kept` keeps the grade. */
export function matches(grade: Evaluation, kept: GradeFilter): boolean {
  if (kept === 'mark') return grade.status === 'draft' && toMark(grade) > 0;
  if (kept === 'draft') return grade.status === 'draft';
  if (kept === 'finalized') return grade.status === 'finalized';
  return true;
}

/* the first draft after the grade `id` in the list, wrapping to earlier ones; the first draft when `id` is not in it. */
export function draftAfter(list: readonly Evaluation[], id: string): Evaluation | null {
  const draft = (g: Evaluation) => g.status === 'draft' && g.id !== id;
  const at = list.findIndex((g) => g.id === id);
  if (at < 0) return list.find(draft) ?? null;
  return list.slice(at + 1).find(draft) ?? list.slice(0, at).find(draft) ?? null;
}

/* reads pages of grades with their names; made in an injection context. */
export class GradeReader {
  private readonly grading = inject(GradingApi);
  private readonly identity = inject(IdentityApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  /* the classroom's students' names, read once per classroom (or refresh) rather than for every page. */
  private names$: { classroomId: string; names: Promise<Map<string, string>> } | null = null;

  /* the students' names are read again next time. */
  forget(): void {
    this.names$ = null;
  }

  /* the page of evaluations asked for (or the last one, past the end) with the names of their students, assessments, questions and topics. */
  async read(classroomId: string, assessmentId: string, asked: number): Promise<Grades> {
    const { result, page } = await readPage(
      asked,
      PAGE_SIZE,
      (offset, limit) => this.grading.evaluations(classroomId, assessmentId, offset, limit),
      (r) => !r.evaluations.length,
    );
    const grades = result.evaluations;
    const [students, assessments, counts] = await Promise.all([
      this.studentNames(classroomId),
      assessmentsOf(grades, (id) => this.assessmentApi.get(id)),
      assessmentId ? this.grading.counts(classroomId, assessmentId) : null,
      // without the curriculum, topics show as ids.
      this.curriculum.need(grades.flatMap((g) => g.evidence.map((e) => e.topic_id))).catch(() => undefined),
    ]);
    return {
      grades,
      page,
      total: result.total,
      unreleased: counts?.unreleased ?? 0,
      ready: counts?.ready ?? 0,
      students,
      assessments,
    };
  }

  /* a grade not on the page, with its assessment's questions (added to `grades`) and its topics' names, so it can be marked. */
  async grade(
    id: string,
    classroomId: string,
    assessmentId: string,
    grades: Section<Grades>,
  ): Promise<Evaluation> {
    const grade = await this.grading.evaluation(id);
    if (grade.classroom_id !== classroomId || (assessmentId && grade.assessment_id !== assessmentId))
      throw new Error('The grade could not be opened.');
    if (!grades.value()?.assessments.has(grade.assessment_id)) {
      const assessment = await this.assessmentApi.get(grade.assessment_id);
      const value = grades.value();
      if (value)
        grades.value.set({
          ...value,
          assessments: new Map(value.assessments).set(grade.assessment_id, assessment),
        });
    }
    await this.curriculum.need(grade.evidence.map((e) => e.topic_id)).catch(() => undefined);
    return grade;
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
}
