import type { Evaluation, EvaluationPage, Evidence, Question, ResitRequest } from '../../api/models';
import { ApiError } from '../../core/gateway';
import { messageOf } from '../../shared/errors';

/* the resit builder's rules (adr 0017): suggestions from finalized grades, and the server's limits. */

export const RESIT_STUDENTS = 100;
export const RESIT_QUESTIONS = 50;
/* suggestions read at most five pages of 100 grades. */
const GRADE_PAGES = 5;
const GRADE_PAGE = 100;

/* the original's finalized grades, and whether more grades were left unread. */
export interface FinalizedGrades {
  grades: Evaluation[];
  partial: boolean;
}

/* reads the original's grades page by page and keeps the finalized ones: a draft shows no marks yet. */
export async function finalizedGrades(
  fetch: (offset: number, limit: number) => Promise<EvaluationPage>,
): Promise<FinalizedGrades> {
  const read: Evaluation[] = [];
  let total = 0;
  for (let page = 0; page < GRADE_PAGES; page++) {
    const result = await fetch(read.length, GRADE_PAGE);
    read.push(...result.evaluations);
    total = result.total;
    if (!result.evaluations.length || read.length >= total) break;
  }
  return { grades: read.filter((e) => e.status === 'finalized'), partial: read.length < total };
}

function lost(evidence: Pick<Evidence, 'awarded' | 'possible'>): boolean {
  return evidence.awarded < evidence.possible;
}

/* the students who lost marks on any question, in the order their grades came. */
export function strugglingStudents(grades: readonly Evaluation[]): string[] {
  return [...new Set(grades.filter((g) => g.evidence.some(lost)).map((g) => g.student_id))];
}

/* the original's questions any of the students lost marks on. */
export function suggestedQuestions(
  grades: readonly Evaluation[],
  students: ReadonlySet<string>,
): Set<string> {
  return new Set(
    grades
      .filter((g) => students.has(g.student_id))
      .flatMap((g) => g.evidence.filter(lost).map((e) => e.question_id)),
  );
}

/* the topics where the students lost marks, from their evidence (or the question it marks); without any, the chosen questions' topics, else all of the original's. */
export function weakTopics(
  grades: readonly Evaluation[],
  students: ReadonlySet<string>,
  questions: readonly Pick<Question, 'id' | 'topic_id'>[],
  copies: ReadonlySet<string>,
): string[] {
  const topicOf = (questionId: string) => questions.find((q) => q.id === questionId)?.topic_id ?? '';
  const topics = grades
    .filter((g) => students.has(g.student_id))
    .flatMap((g) => g.evidence.filter(lost).map((e) => e.topic_id || topicOf(e.question_id)));
  const fallback = questions.filter((q) => copies.has(q.id));
  const found = topics.length ? topics : (fallback.length ? fallback : questions).map((q) => q.topic_id);
  return [...new Set(found.filter(Boolean))];
}

/* what createResit sends: copies in the original's order, then bank questions as chosen; an empty title takes the server's default. */
export function resitRequest(
  students: Iterable<string>,
  questions: readonly Pick<Question, 'id'>[],
  copies: ReadonlySet<string>,
  bank: Iterable<string>,
  title: string,
): ResitRequest {
  const request: ResitRequest = {
    student_ids: [...students],
    copy_question_ids: questions.filter((q) => copies.has(q.id)).map((q) => q.id),
    bank_question_ids: [...bank],
  };
  if (title.trim()) request.title = title.trim();
  return request;
}

const REFUSALS: Record<string, string> = {
  STUDENT_NOT_ENROLLED:
    'A student you chose is no longer in this classroom. Refresh the choices, then create the resit again.',
  STUDENT_DELETED: 'A student you chose has deleted their account. Untick them, then create the resit again.',
  INVALID_BANK_QUESTION:
    'A bank question you chose is no longer in your bank. Untick it, then create the resit again.',
  INVALID_TOPIC:
    'A question you chose has an archived topic. Untick it or restore its topic, then create the resit again.',
  NOT_PUBLISHED: 'This assessment is no longer published, so it cannot have a resit.',
  ASSESSMENT_CHANGED:
    "This assessment's questions changed since you opened it. Refresh the choices, then create the resit again.",
};

/* why the server refused a resit, in words that say what to do next. */
export function resitRefusal(error: unknown): string {
  const known = error instanceof ApiError ? REFUSALS[error.code] : undefined;
  return known ?? messageOf(error, 'The resit could not be created. Retry later.');
}
