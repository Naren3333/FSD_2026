import type { Assessment, Evaluation, Evidence, Question, Student, Topic } from '../api/models';

/** Turns the ids the grading APIs return into the names people recognise. Unknown ids stay ids. */

/** A student's roster name: their full name, else their username; empty until Identity has either. */
export function studentName(student: Student): string {
  return student.display_name || student.username;
}

/** Each student's roster name by id, leaving out students Identity has no name for yet. */
export function studentNames(roster: readonly Student[]): Map<string, string> {
  return new Map(roster.filter((s) => studentName(s)).map((s) => [s.id, studentName(s)]));
}

/** Each curriculum topic's name by id. */
export function topicNames(topics: readonly Topic[]): Map<string, string> {
  return new Map(topics.map((t) => [t.id, t.name]));
}

/** The names in evaluations' evidence, from the assessments they belong to and the curriculum: '' for whatever is
 * unknown. Questions are named only from assessments read with them, not from summaries. */
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
  /** The name of the topic of a piece of evidence, or of a topic's metric. */
  topic(item: Pick<Evidence, 'topic_id'>): string {
    return this.topicName(item.topic_id);
  }
}

/** The marks awarded across an evaluation's questions, out of the marks possible. */
export function total(evidence: readonly Pick<Evidence, 'awarded' | 'possible'>[]): {
  awarded: number;
  possible: number;
} {
  return evidence.reduce(
    (sum, e) => ({ awarded: sum.awarded + e.awarded, possible: sum.possible + e.possible }),
    { awarded: 0, possible: 0 },
  );
}

/** Loads each assessment the evaluations refer to once, by id. */
export async function assessmentsOf(
  evaluations: readonly Pick<Evaluation, 'assessment_id'>[],
  get: (id: string) => Promise<Assessment>,
): Promise<Map<string, Assessment>> {
  const ids = [...new Set(evaluations.map((e) => e.assessment_id))];
  return new Map(await Promise.all(ids.map(async (id) => [id, await get(id)] as const)));
}
