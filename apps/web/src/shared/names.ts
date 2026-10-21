import type { Assessment, Evaluation, Evidence, Question, Student, Topic } from '../api/models';

/* turns the ids the grading apis return into the names people recognise. */

/* a student's roster name: their full name, else their username; empty until identity has either. */
export function studentName(student: Student): string {
  return student.display_name || student.username;
}

/* each student's roster name by id, leaving out students identity has no name for yet. */
export function studentNames(roster: readonly Student[]): Map<string, string> {
  return new Map(roster.filter((s) => studentName(s)).map((s) => [s.id, studentName(s)]));
}

/* each curriculum topic's name by id. */
export function topicNames(topics: readonly Topic[]): Map<string, string> {
  return new Map(topics.map((t) => [t.id, t.name]));
}

/* the names in evaluations' evidence, from the assessments they belong to and the curriculum: '' for whatever is unknown. */
export class EvidenceNames {
  constructor(
    private readonly assessments: () =>
      | ReadonlyMap<string, Pick<Assessment, 'title'> & Partial<Pick<Assessment, 'questions'>>>
      | undefined,
    private readonly topicName: (topicId: string) => string,
  ) {}

  title(evaluation: Pick<Evaluation, 'assessment_id'>): string {
    return this.assessments()?.get(evaluation.assessment_id)?.title ?? '';
  }
  question(
    evaluation: Pick<Evaluation, 'assessment_id'>,
    evidence: Pick<Evidence, 'question_id'>,
  ): Question | undefined {
    return this.assessments()
      ?.get(evaluation.assessment_id)
      ?.questions?.find((q) => q.id === evidence.question_id);
  }
  prompt(evaluation: Pick<Evaluation, 'assessment_id'>, evidence: Pick<Evidence, 'question_id'>): string {
    return this.question(evaluation, evidence)?.prompt ?? '';
  }
  /* the name of the topic of a piece of evidence, or of a topic's metric. */
  topic(item: Pick<Evidence, 'topic_id'>): string {
    return this.topicName(item.topic_id);
  }
}

/* the marks awarded across an evaluation's questions, out of the marks possible. */
export function total(evidence: readonly Pick<Evidence, 'awarded' | 'possible'>[]): {
  awarded: number;
  possible: number;
} {
  return evidence.reduce(
    (sum, e) => ({ awarded: sum.awarded + e.awarded, possible: sum.possible + e.possible }),
    { awarded: 0, possible: 0 },
  );
}

/* loads each assessment the evaluations refer to once, by id. */
export async function assessmentsOf(
  evaluations: readonly Pick<Evaluation, 'assessment_id'>[],
  get: (id: string) => Promise<Assessment>,
): Promise<Map<string, Assessment>> {
  const ids = [...new Set(evaluations.map((e) => e.assessment_id))];
  return new Map(await Promise.all(ids.map(async (id) => [id, await get(id)] as const)));
}
