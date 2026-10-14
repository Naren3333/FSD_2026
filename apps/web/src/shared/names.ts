import type { Assessment, Evaluation, Evidence, Node, Student } from '../api/models';

/** Turns the ids the grading APIs return into the names people recognise. Unknown ids stay ids. */

/** A student's roster name: their full name, else their username; empty until Identity has either. */
export function studentName(student: Student): string {
  return student.display_name || student.username;
}

/** Each student's roster name by id, leaving out students Identity has no name for yet. */
export function studentNames(roster: readonly Student[]): Map<string, string> {
  return new Map(roster.filter((s) => studentName(s)).map((s) => [s.id, studentName(s)]));
}

/** Each curriculum skill's name by id. */
export function skillNames(nodes: readonly Node[]): Map<string, string> {
  return new Map(nodes.filter((n) => n.kind === 'skill').map((n) => [n.id, n.name]));
}

/** The prompt of a question in its assessment, or '' when the assessment or question is unknown. */
export function promptOf(
  assessments: ReadonlyMap<string, Assessment>,
  assessmentId: string,
  questionId: string,
): string {
  return assessments.get(assessmentId)?.questions.find((q) => q.id === questionId)?.prompt ?? '';
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
