import { FormControl, FormGroup, Validators } from '@angular/forms';
import type { Evaluation, Evidence, Mark, Question, Suggestion } from '../../api/models';
import { dateTime, plural } from '../../shared/format';

/* the marking form for one evaluation and what it sends to grading. */

export type QuestionMarks = FormGroup<{
  /* points for a question without a rubric. */
  awarded: FormControl<number | null>;
  /* the chosen level's points for each rubric criterion, by criterion id. */
  criteria: FormGroup<Record<string, FormControl<number | null>>>;
  feedback: FormControl<string>;
}>;
export type MarkingForm = FormGroup<Record<string, QuestionMarks>>;

/* a form holding each question's current marks; a question still to mark must be given them. */
export function markingForm(evidence: readonly Evidence[], questions: readonly Question[]): MarkingForm {
  return new FormGroup(
    Object.fromEntries(
      evidence.map((e) => {
        const rubric = questions.find((q) => q.id === e.question_id)?.rubric;
        const required = e.needs_marking ? [Validators.required] : [];
        const scores = new Map((e.criteria ?? []).map((c) => [c.criterion_id, c.points]));
        // assessment gives every criterion an id; one without could not be marked, so it gets no control.
        const criteria = Object.fromEntries(
          (rubric?.criteria ?? []).flatMap((c) =>
            c.id ? [[c.id, new FormControl<number | null>(scores.get(c.id) ?? null, required)]] : [],
          ),
        );
        const marks: QuestionMarks = new FormGroup({
          awarded: new FormControl<number | null>(rubric || e.needs_marking ? null : e.awarded, [
            ...(rubric ? [] : required),
            Validators.min(0),
            Validators.max(e.possible),
          ]),
          criteria: new FormGroup(criteria),
          feedback: new FormControl(e.feedback ?? '', {
            nonNullable: true,
            validators: [Validators.maxLength(2000)],
          }),
        });
        return [e.question_id, marks];
      }),
    ),
  );
}

/* the marks to send: each question the teacher marked or gave feedback on. */
export function marksOf(form: MarkingForm, evidence: readonly Evidence[]): Record<string, Mark> {
  const marks: Record<string, Mark> = {};
  for (const e of evidence) {
    const value = form.controls[e.question_id]?.getRawValue();
    if (!value) continue;
    const mark: Mark = {};
    const levels = Object.entries(value.criteria);
    if (levels.length && levels.every(([, points]) => points !== null)) {
      const chosen = Object.fromEntries(levels) as Record<string, number>;
      const unchanged =
        !e.needs_marking &&
        (e.criteria ?? []).length === levels.length &&
        (e.criteria ?? []).every((c) => chosen[c.criterion_id] === c.points);
      if (!unchanged) mark.criteria = chosen;
    } else if (!levels.length && value.awarded !== null && (e.needs_marking || value.awarded !== e.awarded)) {
      mark.awarded = Number(value.awarded);
    }
    if (value.feedback !== (e.feedback ?? '')) mark.feedback = value.feedback;
    if (Object.keys(mark).length) marks[e.question_id] = mark;
  }
  return marks;
}

/* puts a ready suggestion's levels (or points) and feedback into the question's form, for the teacher to check. */
export function applySuggestion(marks: QuestionMarks, suggestion: Suggestion): void {
  const criteria = suggestion.criteria ?? [];
  const rubric = Object.keys(marks.controls.criteria.controls).length > 0;
  if (rubric) {
    for (const c of criteria) marks.controls.criteria.controls[c.criterion_id]?.setValue(c.points);
  } else {
    marks.controls.awarded.setValue(suggestion.awarded);
  }
  if (suggestion.feedback) marks.controls.feedback.setValue(suggestion.feedback);
  marks.markAsDirty();
}

/* why an ai call waits, in the teacher's words; for a suggestion and its second check alike. */
const WAITING: Record<string, string> = {
  AI_SPEND_LIMIT: 'Waiting for your daily AI allowance to reset.',
  AI_BUSY: 'Waiting for your other AI requests to finish.',
  AI_PROVIDER_NOT_CONFIGURED: 'AI is not set up on this server yet. It will be tried again every 10 minutes.',
  AI_PROVIDER_FAILED: 'The AI provider failed; it will be tried again.',
};

/* why a suggestion is not ready, in the teacher's words. */
export function suggestionReason(suggestion: Suggestion): string {
  const reasons: Record<string, string> = {
    ...WAITING,
    AI_OUTPUT_REJECTED:
      "The AI's reply could not be used, so there is no suggestion. Mark this question yourself.",
    CONTRADICTED:
      'The AI gave marks while quoting parts of the answer that count against them, such as competing answers, so there is no suggestion. Mark this question yourself.',
    MARKED_BY_TEACHER: 'You have already marked this question, so no suggestion was made.',
    MARKING_INTERRUPTED: 'Preparing the suggestion was interrupted. Mark this question yourself.',
    BLANK_ANSWER: 'The answer is blank, so there is nothing to suggest.',
    NOT_MARKABLE: 'This question cannot have a suggestion.',
    STUDENT_REMOVED:
      'The student was removed from the classroom before this was marked, so no suggestion was made. Mark this question yourself.',
  };
  if (suggestion.status === 'pending')
    return suggestion.reason
      ? (reasons[suggestion.reason] ?? `Waiting to try again (${suggestion.reason}).`)
      : 'A suggestion is being prepared.';
  if (suggestion.status === 'failed')
    return reasons[suggestion.reason ?? ''] ?? 'No suggestion could be made. Mark this question yourself.';
  return '';
}

/* flags a suggestion carries, in words. */
export function flagText(flag: string): string {
  const flags: Record<string, string> = {
    blank: 'The answer looks blank.',
    off_topic: 'The answer may be off topic.',
    possible_instructions_in_answer: 'The answer may try to instruct the marker. Check it closely.',
    language_mismatch: 'The answer is not in the language of the question.',
    feedback_withheld: 'Feedback was withheld because it repeated the model answer.',
  };
  return flags[flag] ?? flag;
}

/* how many of a grade's questions still need marking. */
export function toMark(grade: Evaluation): number {
  return grade.evidence.filter((e) => e.needs_marking).length;
}

/* how a question of a finalized grade was marked, in words. */
export function outcome(evidence: Evidence): string {
  if (evidence.needs_marking) return 'Needs marking';
  if (evidence.marked_by === 'teacher') return 'Marked by you';
  return evidence.correct ? 'Correct' : 'Incorrect';
}

export function submittedAt(grade: Evaluation): string {
  return grade.submitted_at ? dateTime(grade.submitted_at) : '';
}

/* a correct answer marked automatically, shown as one line until the teacher overrides it. */
export function collapsible(evidence: Evidence): boolean {
  return !evidence.needs_marking && evidence.marked_by === 'auto' && evidence.correct;
}

/* the marks the form gives so far, and how many questions still need marking. */
export function markingSummary(grade: Evaluation, form: MarkingForm): string {
  let awarded = 0;
  let possible = 0;
  let left = 0;
  for (const e of grade.evidence) {
    possible += e.possible;
    const points = formPoints(form, e);
    if (points !== null) awarded += points;
    else if (e.needs_marking) left++;
    else awarded += e.awarded;
  }
  const rest = left
    ? `${left} ${left === 1 ? 'question' : 'questions'} left to mark`
    : 'every question marked';
  return `${awarded} of ${possible} marks · ${rest}`;
}

/* the points the form gives a question so far, or null while it gives none. */
function formPoints(form: MarkingForm, evidence: Evidence): number | null {
  const value = form.controls[evidence.question_id]?.getRawValue();
  const levels = Object.values(value?.criteria ?? {});
  if (levels.length)
    return levels.every((p) => p !== null) ? levels.reduce<number>((sum, p) => sum + Number(p), 0) : null;
  return value?.awarded == null ? null : Number(value.awarded);
}

/* a suggested rubric level, named by its criterion and descriptor. */
export function levelName(question: Question | undefined, criterionId: string, points: number): string {
  const level = question?.rubric?.criteria
    .find((c) => c.id === criterionId)
    ?.levels.find((l) => l.points === points);
  return `${criterionTitle(question, criterionId)}: ${points} (${level?.descriptor ?? 'level'})`;
}

/* a rubric criterion's title; a short answer's single criterion is its mark. */
export function criterionTitle(question: Question | undefined, criterionId: string): string {
  return (
    question?.rubric?.criteria.find((c) => c.id === criterionId)?.title ??
    (criterionId === 'match' ? 'Mark' : 'Criterion')
  );
}

/* why a suggestion was not second-checked, in the teacher's words. */
const NOT_REVIEWED: Record<string, string> = {
  AI_OUTPUT_REJECTED: 'the second marking could not be used.',
  EMPTY_OUTPUT: 'the second marking could not be used.',
  INVALID_OUTPUT: 'the second marking could not be used.',
  UNGROUNDED: 'the second marking quoted words that are not in the answer.',
  CONTRADICTED: 'the second marking contradicted itself.',
  AI_PROVIDER_FAILED: 'the AI provider failed.',
  MARKED_BY_TEACHER: 'you marked this question differently first.',
  STUDENT_REMOVED: 'the student was removed from the classroom.',
  MARKING_INTERRUPTED: 'the second check was interrupted.',
  AI_TIMEOUT: 'the second marking took too long.',
  AI_UNAVAILABLE: 'AI was unavailable.',
  BLANK_ANSWER: 'the answer is blank.',
  NOT_MARKABLE: 'this question cannot be second-checked.',
  CRITIC_OFF: 'the second check is turned off on this server.',
  FINALIZED: 'the grade was finalized first.',
  NO_TEACHER: 'no teacher is assigned to pay for it.',
};

/* a criterion of a doubtful suggestion where the second check chose another level, with its rationale. */
export interface ReviewDifference {
  criterion_id: string;
  suggested: number;
  second: number;
  rationale: string;
}

/* the criteria the second check marked differently from the suggestion. */
export function reviewDifferences(suggestion: Suggestion): ReviewDifference[] {
  const suggested = new Map((suggestion.criteria ?? []).map((c) => [c.criterion_id, c.points]));
  return (suggestion.review?.criteria ?? []).flatMap((c) => {
    const points = suggested.get(c.criterion_id);
    return points === undefined || points === c.points
      ? []
      : [{ criterion_id: c.criterion_id, suggested: points, second: c.points, rationale: c.rationale }];
  });
}

/* what the second check of a ready suggestion found, in the teacher's words; '' without one. */
export function reviewText(suggestion: Suggestion, question?: Question): string {
  const review = suggestion.review;
  if (!review) return '';
  if (review.status === 'pending')
    return review.reason
      ? `Second check waiting. ${WAITING[review.reason] ?? `It will be tried again (${review.reason}).`}`
      : 'Second check running.';
  if (review.status === 'not_reviewed')
    return `Not second-checked: ${NOT_REVIEWED[review.reason ?? ''] ?? 'it could not be made.'}`;
  if (review.status === 'agreed') return 'Second check agrees.';
  const differences = reviewDifferences(suggestion);
  const doubts = differences.map(
    (d) => `${criterionTitle(question, d.criterion_id)} ${d.suggested} vs ${d.second}`,
  );
  const reasons = review.reasons ?? [];
  if (reasons.includes('LOW_CONFIDENCE')) doubts.push('one of the two markings was unsure');
  if (reasons.includes('CRITIC_FLAGGED')) doubts.push('it raised a flag');
  const heading = differences.length ? 'Second check disagrees' : 'Second check has doubts';
  return `${heading}: ${doubts.join('; ') || 'check this mark'}.`;
}

/* how many of a grade's questions have a suggestion the second check doubted that the marks still follow: not
   marked yet, or giving its total. form, when given, holds marks not saved yet. */
export function doubtful(grade: Evaluation, form?: MarkingForm): number {
  return grade.evidence.filter((e) => {
    const s = e.suggestion;
    if (s?.review?.status !== 'doubtful') return false;
    const points = form ? formPoints(form, e) : null;
    if (points !== null) return points === s.awarded;
    return !!e.needs_marking || e.awarded === s.awarded;
  }).length;
}

/* a draft with a suggestion the second check doubted, which the queue flags and lists first. */
export function needsCheck(grade: Evaluation): boolean {
  return grade.status === 'draft' && doubtful(grade) > 0;
}

/* the finalize dialogs' note on n doubtful suggestions, counted as `counted`; '' without any. */
export function doubtfulNote(n: number, counted = plural(n, 'question')): string {
  if (!n) return '';
  return `${counted} ${n === 1 ? 'has' : 'have'} a doubtful AI suggestion. Check ${n === 1 ? 'it' : 'them'} before finalizing.`;
}
