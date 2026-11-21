import { describe, expect, it } from 'vitest';
import type { Evaluation, Evidence, Question, Suggestion } from '../../api/models';
import { doubtfulFirst, inOrder } from './grades';
import {
  applySuggestion,
  doubtful,
  doubtfulNote,
  markingForm,
  marksOf,
  reviewDifferences,
  reviewText,
  suggestionReason,
} from './marking';

const rubricQuestion: Question = {
  id: 'free',
  prompt: 'Explain.',
  type: 'free_response',
  options: [],
  points: 5,
  topic_id: 's1',
  rubric: {
    criteria: [
      {
        id: 'c1',
        title: 'Accuracy',
        levels: [
          { points: 0, descriptor: 'Wrong' },
          { points: 4, descriptor: 'Right' },
        ],
      },
      {
        id: 'c2',
        title: 'Clarity',
        levels: [
          { points: 0, descriptor: 'Unclear' },
          { points: 1, descriptor: 'Clear' },
        ],
      },
    ],
  },
};
const evidence: Evidence[] = [
  { question_id: 'auto', topic_id: 's1', correct: true, awarded: 1, possible: 1, marked_by: 'auto' },
  { question_id: 'short', topic_id: 's1', correct: false, awarded: 0, possible: 2, needs_marking: true },
  { question_id: 'free', topic_id: 's1', correct: false, awarded: 0, possible: 5, needs_marking: true },
];

describe('marking form', () => {
  it('requires marks for questions still to mark and keeps automatic marks', () => {
    const form = markingForm(evidence, [rubricQuestion]);
    expect(form.controls['auto'].controls.awarded.value).toBe(1);
    expect(form.valid).toBe(false);
    form.controls['short'].controls.awarded.setValue(2);
    form.controls['free'].controls.criteria.setValue({ c1: 4, c2: 0 });
    expect(form.valid).toBe(true);
    form.controls['short'].controls.awarded.setValue(3);
    expect(form.valid).toBe(false); // above the question's marks
  });

  it('sends only what changed: new marks, overrides and feedback', () => {
    const form = markingForm(evidence, [rubricQuestion]);
    expect(marksOf(form, evidence)).toEqual({});
    form.controls['short'].controls.awarded.setValue(2);
    form.controls['free'].controls.criteria.setValue({ c1: 4, c2: null });
    form.controls['auto'].controls.feedback.setValue('Well done.');
    // a rubric with a criterion left blank is not sent.
    expect(marksOf(form, evidence)).toEqual({ short: { awarded: 2 }, auto: { feedback: 'Well done.' } });
    form.controls['free'].controls.criteria.setValue({ c1: 4, c2: 1 });
    form.controls['auto'].controls.awarded.setValue(0);
    expect(marksOf(form, evidence)).toEqual({
      short: { awarded: 2 },
      auto: { awarded: 0, feedback: 'Well done.' },
      free: { criteria: { c1: 4, c2: 1 } },
    });
  });

  it('does not resend a rubric mark that is already saved', () => {
    const marked: Evidence[] = [
      {
        ...evidence[2],
        needs_marking: false,
        awarded: 5,
        criteria: [
          { criterion_id: 'c1', points: 4 },
          { criterion_id: 'c2', points: 1 },
        ],
      },
    ];
    const form = markingForm(marked, [rubricQuestion]);
    expect(form.controls['free'].controls.criteria.value).toEqual({ c1: 4, c2: 1 });
    expect(marksOf(form, marked)).toEqual({});
  });

  it('puts a suggestion into the form for the teacher to check', () => {
    const form = markingForm(evidence, [rubricQuestion]);
    applySuggestion(form.controls['free'], {
      status: 'ready',
      awarded: 4,
      feedback: 'Say more about clarity.',
      criteria: [
        { criterion_id: 'c1', points: 4, evidence_quotes: ['light'], rationale: 'Right idea.' },
        { criterion_id: 'c2', points: 0, evidence_quotes: [], rationale: 'Hard to follow.' },
      ],
    });
    applySuggestion(form.controls['short'], { status: 'ready', awarded: 2 });
    expect(marksOf(form, evidence)).toEqual({
      short: { awarded: 2 },
      free: { criteria: { c1: 4, c2: 0 }, feedback: 'Say more about clarity.' },
    });
  });

  it('explains suggestions that are not ready', () => {
    expect(suggestionReason({ status: 'pending', awarded: 0 })).toBe('A suggestion is being prepared.');
    expect(suggestionReason({ status: 'pending', awarded: 0, reason: 'AI_SPEND_LIMIT' })).toContain(
      'allowance',
    );
    expect(suggestionReason({ status: 'pending', awarded: 0, reason: 'AI_BUSY' })).toContain(
      'other AI requests',
    );
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'invalid quote' })).toContain('yourself');
    expect(
      suggestionReason({ status: 'pending', awarded: 0, reason: 'AI_PROVIDER_NOT_CONFIGURED' }),
    ).toContain('tried again');
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'MARKED_BY_TEACHER' })).toContain(
      'already marked',
    );
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'CONTRADICTED' })).toContain(
      'competing answers',
    );
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'AI_OUTPUT_REJECTED' })).toContain(
      'yourself',
    );
    expect(suggestionReason({ status: 'failed', awarded: 0, reason: 'STUDENT_REMOVED' })).toContain(
      'removed from the classroom',
    );
  });
});

const suggested: Suggestion = {
  status: 'ready',
  awarded: 5,
  confidence: 'high',
  criteria: [
    { criterion_id: 'c1', points: 4, evidence_quotes: ['light'], rationale: 'Right idea.' },
    { criterion_id: 'c2', points: 1, evidence_quotes: ['clear'], rationale: 'Clear.' },
  ],
};
const reviewed = (review: Suggestion['review']): Suggestion => ({ ...suggested, review });

describe("the marking critic's second check", () => {
  it('says what it found in words', () => {
    expect(reviewText(suggested)).toBe('');
    expect(reviewText(reviewed({ status: 'pending', awarded: 0 }))).toBe('Second check running.');
    expect(reviewText(reviewed({ status: 'pending', awarded: 0, reason: 'AI_SPEND_LIMIT' }))).toBe(
      'Second check waiting. Waiting for your daily AI allowance to reset.',
    );
    expect(reviewText(reviewed({ status: 'agreed', awarded: 5 }))).toBe('Second check agrees.');
    expect(reviewText(reviewed({ status: 'not_reviewed', awarded: 0, reason: 'UNGROUNDED' }))).toBe(
      'Not second-checked: the second marking quoted words that are not in the answer.',
    );
    expect(reviewText(reviewed({ status: 'not_reviewed', awarded: 0, reason: 'SOMETHING' }))).toBe(
      'Not second-checked: it could not be made.',
    );
  });

  it('names the criteria it marked differently, with both levels', () => {
    const doubt = reviewed({
      status: 'doubtful',
      awarded: 1,
      reasons: ['LEVELS_DIFFER', 'LOW_CONFIDENCE'],
      criteria: [
        { criterion_id: 'c1', points: 0, rationale: 'Not explained.' },
        { criterion_id: 'c2', points: 1, rationale: 'Clear.' },
      ],
    });
    expect(reviewDifferences(doubt)).toEqual([
      { criterion_id: 'c1', suggested: 4, second: 0, rationale: 'Not explained.' },
    ]);
    expect(reviewText(doubt, rubricQuestion)).toBe(
      'Second check disagrees: Accuracy 4 vs 0; one of the two markings was unsure.',
    );
    const flagged = reviewed({
      status: 'doubtful',
      awarded: 5,
      reasons: ['CRITIC_FLAGGED'],
      flags: ['off_topic'],
    });
    expect(reviewText(flagged, rubricQuestion)).toBe('Second check has doubts: it raised a flag.');
  });

  it('warns before finalizing a grade with doubtful suggestions, and sorts those drafts first', () => {
    const grade = (id: string, status: Evaluation['status'], review?: Suggestion['review']): Evaluation => ({
      id,
      assessment_id: 'a1',
      classroom_id: 'c1',
      student_id: id,
      status,
      evidence: [{ ...evidence[2], suggestion: reviewed(review) }],
      answers: {},
      late: false,
    });
    const doubt = { status: 'doubtful' as const, awarded: 0, reasons: ['LEVELS_DIFFER' as const] };
    const list = [
      grade('g1', 'draft', { status: 'agreed', awarded: 5 }),
      grade('g2', 'finalized', doubt),
      grade('g3', 'draft', doubt),
      grade('g4', 'draft'),
      grade('g5', 'draft', doubt),
    ];
    expect(doubtful(list[2])).toBe(1);
    expect(doubtfulNote(doubtful(list[0]))).toBe('');
    expect(doubtfulNote(doubtful(list[2]))).toBe(
      '1 question has a doubtful AI suggestion. Check it before finalizing.',
    );
    const two = { ...list[2], evidence: [list[2].evidence[0], { ...list[2].evidence[0], question_id: 'x' }] };
    expect(doubtfulNote(doubtful(two))).toBe(
      '2 questions have a doubtful AI suggestion. Check them before finalizing.',
    );
    // a doubt stops counting once the teacher's saved mark gives another total than the suggestion's.
    const suggestedTotal = list[2].evidence[0].suggestion!.awarded;
    const marked = (awarded: number): Evaluation => ({
      ...list[2],
      evidence: [{ ...list[2].evidence[0], needs_marking: false, marked_by: 'teacher', awarded }],
    });
    expect(doubtful(marked(suggestedTotal))).toBe(1);
    expect(doubtful(marked(suggestedTotal + 1))).toBe(0);
    // only drafts move, and otherwise the order is kept.
    expect(doubtfulFirst(list).map((g) => g.id)).toEqual(['g3', 'g5', 'g1', 'g2', 'g4']);
    // a grade finalized since keeps the place it had.
    const order = doubtfulFirst(list).map((g) => g.id);
    const after = list.map((g) => (g.id === 'g3' ? { ...g, status: 'finalized' as const } : g));
    expect(inOrder(after, order).map((g) => g.id)).toEqual(['g3', 'g5', 'g1', 'g2', 'g4']);
  });
});
