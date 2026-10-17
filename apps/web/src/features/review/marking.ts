import { FormControl, FormGroup, Validators } from '@angular/forms';
import type { Evidence, Mark, Question, Suggestion } from '../../api/models';

/** The marking form for one evaluation and what it sends to Grading. Kept apart from the page so it can be tested
 * alone. */

export type QuestionMarks = FormGroup<{
  /** Points for a question without a rubric. */
  awarded: FormControl<number | null>;
  /** The chosen level's points for each rubric criterion, by criterion id. */
  criteria: FormGroup<Record<string, FormControl<number | null>>>;
  feedback: FormControl<string>;
}>;
export type MarkingForm = FormGroup<Record<string, QuestionMarks>>;

/** A form holding each question's current marks; a question still to mark must be given them. */
export function markingForm(evidence: readonly Evidence[], questions: readonly Question[]): MarkingForm {
  return new FormGroup(
    Object.fromEntries(
      evidence.map((e) => {
        const rubric = questions.find((q) => q.id === e.question_id)?.rubric;
        const required = e.needs_marking ? [Validators.required] : [];
        const scores = new Map((e.criteria ?? []).map((c) => [c.criterion_id, c.points]));
        const criteria = Object.fromEntries(
          (rubric?.criteria ?? []).map((c) => [
            c.id!,
            new FormControl<number | null>(scores.get(c.id!) ?? null, required),
          ]),
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

/** The marks to send: each question the teacher marked or gave feedback on. A rubric question counts once every
 * criterion has a level; points count when they differ from what the question has, or it still needed marking. */
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

/** Puts a ready suggestion's levels (or points) and feedback into the question's form, for the teacher to check. */
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

/** Why a suggestion is not ready, in the teacher's words. */
export function suggestionReason(suggestion: Suggestion): string {
  const reasons: Record<string, string> = {
    AI_SPEND_LIMIT: 'Waiting for your daily AI allowance to reset.',
    AI_PROVIDER_NOT_CONFIGURED:
      'AI is not set up on this server yet. It will be tried again every 10 minutes.',
    AI_PROVIDER_FAILED: 'The AI provider failed; it will be tried again.',
    AI_OUTPUT_REJECTED:
      "The AI's reply could not be used, so there is no suggestion. Mark this question yourself.",
    MARKED_BY_TEACHER: 'You have already marked this question, so no suggestion was made.',
    MARKING_INTERRUPTED: 'Preparing the suggestion was interrupted. Mark this question yourself.',
    BLANK_ANSWER: 'The answer is blank, so there is nothing to suggest.',
    NOT_MARKABLE: 'This question cannot have a suggestion.',
  };
  if (suggestion.status === 'pending')
    return suggestion.reason
      ? (reasons[suggestion.reason] ?? `Waiting to try again (${suggestion.reason}).`)
      : 'A suggestion is being prepared.';
  if (suggestion.status === 'failed')
    return reasons[suggestion.reason ?? ''] ?? 'No suggestion could be made. Mark this question yourself.';
  return '';
}

/** Flags a suggestion carries, in words. */
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
