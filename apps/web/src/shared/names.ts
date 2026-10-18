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
 * unknown. */
export class EvidenceNames {
  constructor(
    private readonly assessments: () => ReadonlyMap<string, Assessment> | undefined,
    private readonly topicName: (topicId: string) => string,
  ) {}

  title(evaluation: Evaluation): string {
    return this.assessments()?.get(evaluation.assessment_id)?.title ?? '';
  }
  question(evaluation: Evaluation, evidence: Evidence): Question | undefined {
    return this.assessments()
      ?.get(evaluation.assessment_id)
      ?.questions.find((q) => q.id === evidence.question_id);
  }
  prompt(evaluation: Evaluation, evidence: Evidence): string {
    return this.question(evaluation, evidence)?.prompt ?? '';
  }
  topic(evidence: Evidence): string {
    return this.topicName(evidence.topic_id);
  }
}

/** The marks awarded across an evaluation's questions, out of the marks possible. */
export function total(evidence: readonly Evidence[]): { awarded: number; possible: number } {
  return evidence.reduce(
    (sum, e) => ({ awarded: sum.awarded + e.awarded, possible: sum.possible + e.possible }),
    { awarded: 0, possible: 0 },
  );
}

/** Loads each assessment the evaluations refer to once, by id. */
export async function assessmentsOf(
  evaluations: readonly Evaluation[],
  get: (id: string) => Promise<Assessment>,
): Promise<Map<string, Assessment>> {
  const ids = [...new Set(evaluations.map((e) => e.assessment_id))];
  return new Map(await Promise.all(ids.map(async (id) => [id, await get(id)] as const)));
}
