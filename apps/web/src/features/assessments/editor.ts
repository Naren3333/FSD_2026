import {
  AbstractControl,
  FormArray,
  FormControl,
  FormGroup,
  ValidationErrors,
  ValidatorFn,
  Validators,
} from '@angular/forms';
import type {
  Assessment,
  AssessmentDetails,
  AssessmentSettings,
  Criterion,
  DocumentRecord,
  Excerpt,
  LatePolicy,
  NewAssessment,
  Question,
  QuestionType,
  ReleasePolicy,
} from '../../api/models';
import { requiredText } from '../../shared/validators';

/** The assessment editor's form and its conversion to and from what Assessment stores. Limits mirror Assessment's
 * own checks so a teacher hears about a problem before saving; Assessment still decides. */

export const MAX_QUESTIONS = 50;
export const MAX_CRITERIA = 10;
/** Marking points (the questions' `marking_scheme` passages): per question, characters per point, and per
 * assessment. */
export const MAX_EXCERPTS = 5;
export const MAX_EXCERPT_TEXT = 2000;
export const MAX_ASSESSMENT_EXCERPTS = 20;
export const MAX_PROMPT = 2000;
export const MAX_ACCEPTED = 20;
const DEFAULT_MAX_LENGTH = 5000;

export type CriterionForm = FormGroup<{
  title: FormControl<string>;
  /** One level per line, "points: description". */
  levels: FormControl<string>;
}>;

export type QuestionForm = FormGroup<{
  prompt: FormControl<string>;
  type: FormControl<QuestionType>;
  topic_id: FormControl<string>;
  points: FormControl<number>;
  options: FormControl<string>;
  answer: FormControl<string>;
  tolerance: FormControl<string>;
  accepted: FormControl<string>;
  case_sensitive: FormControl<boolean>;
  max_length: FormControl<number>;
  model_answer: FormControl<string>;
  criteria: FormArray<CriterionForm>;
  /** Short answers and free responses: marking points for AI suggestions. */
  marking_scheme: FormControl<Excerpt[]>;
  /** Generated questions: the passages they were drawn from, kept for the teacher (ADR 0006). */
  sources: FormControl<Excerpt[]>;
  origin: FormControl<'' | 'generated'>;
}>;

export type AssessmentForm = FormGroup<{
  title: FormControl<string>;
  opens_at: FormControl<string>;
  due_at: FormControl<string>;
  late_policy: FormControl<LatePolicy>;
  release_policy: FormControl<ReleasePolicy>;
  ai_marking: FormControl<boolean>;
  questions: FormArray<QuestionForm>;
}>;

/** Multiple-choice options or accepted answers: one per non-blank line. */
export function optionLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}

/** A criterion's levels from lines such as "2: Partly right", or null when a line cannot be read. */
export function parseLevels(text: string): { points: number; descriptor: string }[] | null {
  const levels = [];
  for (const line of optionLines(text)) {
    const match = /^(\d{1,3})\s*[:|-]\s*(.+)$/.exec(line);
    if (!match) return null;
    levels.push({ points: Number(match[1]), descriptor: match[2].trim() });
  }
  return levels;
}

export function levelLines(criterion: Criterion): string {
  return criterion.levels.map((l) => `${l.points}: ${l.descriptor}`).join('\n');
}

/** 2–6 levels, each "points: description" with its own points from 0 to 100. */
const levelsValidator: ValidatorFn = (control: AbstractControl<string>): ValidationErrors | null => {
  const levels = parseLevels(control.value);
  if (!levels || levels.length < 2 || levels.length > 6) return { levels: true };
  const points = levels.map((l) => l.points);
  if (points.some((p) => p > 100) || new Set(points).size !== points.length) return { levels: true };
  if (levels.some((l) => l.descriptor.length > 500)) return { levels: true };
  return null;
};

/** The highest level of each criterion, added up: a free response's points. */
export function rubricTotal(criteria: readonly { levels: string }[]): number {
  return criteria.reduce((sum, c) => {
    const levels = parseLevels(c.levels) ?? [];
    return sum + Math.max(0, ...levels.map((l) => l.points));
  }, 0);
}

export function criterionForm(criterion?: Criterion): CriterionForm {
  return new FormGroup({
    title: new FormControl(criterion?.title ?? '', { nonNullable: true, validators: requiredText(200) }),
    levels: new FormControl(
      criterion ? levelLines(criterion) : '0: Not shown\n1: Partly shown\n2: Fully shown',
      {
        nonNullable: true,
        validators: [levelsValidator],
      },
    ),
  });
}

export function questionForm(question?: Omit<Question, 'id'>): QuestionForm {
  const form: QuestionForm = new FormGroup({
    prompt: new FormControl(question?.prompt ?? '', {
      nonNullable: true,
      validators: requiredText(MAX_PROMPT),
    }),
    type: new FormControl<QuestionType>(question?.type ?? 'numerical', { nonNullable: true }),
    topic_id: new FormControl(question?.topic_id ?? '', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    points: new FormControl(question?.points ?? 1, {
      nonNullable: true,
      validators: [Validators.required, Validators.min(1), Validators.max(100)],
    }),
    options: new FormControl((question?.options ?? []).join('\n'), { nonNullable: true }),
    answer: new FormControl(question?.answer ?? '', { nonNullable: true }),
    tolerance: new FormControl(question?.tolerance || '0', { nonNullable: true }),
    accepted: new FormControl((question?.accepted_answers ?? []).join('\n'), { nonNullable: true }),
    case_sensitive: new FormControl(question?.case_sensitive ?? false, { nonNullable: true }),
    max_length: new FormControl(question?.max_length ?? DEFAULT_MAX_LENGTH, {
      nonNullable: true,
      validators: [Validators.required, Validators.min(1), Validators.max(20000)],
    }),
    model_answer: new FormControl(question?.rubric?.model_answer ?? '', {
      nonNullable: true,
      validators: [Validators.maxLength(5000)],
    }),
    criteria: new FormArray<CriterionForm>(
      (question?.rubric?.criteria ?? [{ title: '', levels: [] }]).map((c) =>
        criterionForm(c.levels.length ? c : undefined),
      ),
    ),
    marking_scheme: new FormControl<Excerpt[]>(question?.marking_scheme ?? [], {
      nonNullable: true,
      validators: [schemeValidator],
    }),
    sources: new FormControl<Excerpt[]>(question?.sources ?? [], { nonNullable: true }),
    origin: new FormControl<'' | 'generated'>(question?.origin ?? '', { nonNullable: true }),
  });
  applyType(form);
  form.controls.type.valueChanges.subscribe(() => applyType(form));
  return form;
}

/** Turns on the controls the question's type uses and turns off the rest, so only they are validated. */
function applyType(form: QuestionForm): void {
  const type = form.controls.type.value;
  const c = form.controls;
  const uses: Record<string, boolean> = {
    points: type !== 'free_response',
    options: type === 'multiple_choice',
    answer: type === 'multiple_choice' || type === 'numerical',
    tolerance: type === 'numerical',
    accepted: type === 'short_answer',
    case_sensitive: type === 'short_answer',
    max_length: type === 'free_response',
    model_answer: type === 'free_response',
    criteria: type === 'free_response',
    marking_scheme: type === 'short_answer' || type === 'free_response',
  };
  c.answer.setValidators(uses['answer'] ? [Validators.required, Validators.maxLength(500)] : []);
  c.options.setValidators(uses['options'] ? [optionsValidator] : []);
  c.accepted.setValidators(uses['accepted'] ? [acceptedValidator] : []);
  for (const [name, used] of Object.entries(uses)) {
    const control = form.get(name)!;
    if (used) control.enable({ emitEvent: false });
    else control.disable({ emitEvent: false });
  }
  c.answer.updateValueAndValidity({ emitEvent: false });
  c.options.updateValueAndValidity({ emitEvent: false });
  c.accepted.updateValueAndValidity({ emitEvent: false });
}

/** At most five marking points, each with 1–2,000 characters of text. */
const schemeValidator: ValidatorFn = (control: AbstractControl<Excerpt[]>) => {
  const excerpts = control.value;
  return excerpts.length <= MAX_EXCERPTS &&
    excerpts.every((e) => e.text.trim() && characters(e.text) <= MAX_EXCERPT_TEXT)
    ? null
    : { scheme: true };
};

/** Characters as Assessment counts them (code points, not UTF-16 units). */
export function characters(text: string): number {
  return Array.from(text).length;
}

/** The text cut to at most `limit` characters. */
export function clipText(text: string, limit: number): string {
  return characters(text) <= limit ? text : Array.from(text).slice(0, limit).join('');
}

/** Marking points across the assessment's questions, counting only questions that keep them. */
export function schemeTotal(
  questions: readonly { type: QuestionType; marking_scheme?: readonly Excerpt[] }[],
): number {
  return questions.reduce((sum, q) => sum + (usesScheme(q.type) ? (q.marking_scheme?.length ?? 0) : 0), 0);
}

/** At most twenty marking points across the assessment's questions. */
const assessmentSchemeValidator: ValidatorFn = (control: AbstractControl) =>
  schemeTotal((control as FormArray<QuestionForm>).getRawValue()) <= MAX_ASSESSMENT_EXCERPTS
    ? null
    : { schemeTotal: true };

/** Whether a question is still as the editor made it: no wording and nothing the teacher set. */
export function untouched(question: QuestionForm): boolean {
  return question.pristine && !question.controls.prompt.value.trim();
}

/** "1 mark" or "N marks". */
export function marksText(points: number): string {
  return points === 1 ? '1 mark' : `${points} marks`;
}

/** Who wrote each of a question's marking points, for this session: AI (drafted guidance or a kept generated
 * question) or the teacher. Neither carries a document, so once the assessment is reloaded they can no longer be told
 * apart. */
const writtenBy = new WeakMap<FormControl<Excerpt[]>, Map<string, 'ai' | 'teacher'>>();

export function markWrittenBy(control: FormControl<Excerpt[]>, excerpt: Excerpt, by: 'ai' | 'teacher'): void {
  if (excerpt.document_id) return;
  writtenBy.set(control, new Map([...(writtenBy.get(control) ?? []), [excerpt.text, by]]));
}

/** Where a passage is from: its document and page, or that AI or the teacher wrote it, or, when that is no longer
 * known, that it was written for the question. */
export function excerptLabel(
  excerpt: Excerpt,
  documents: readonly DocumentRecord[] | null | undefined,
  scheme?: FormControl<Excerpt[]>,
): string {
  if (!excerpt.document_id) {
    const by = scheme && writtenBy.get(scheme)?.get(excerpt.text);
    return by === 'ai' ? 'Written by AI' : by === 'teacher' ? 'Written by you' : 'Written for this question';
  }
  const title = documents?.find((d) => d.id === excerpt.document_id)?.title ?? 'Class document';
  return excerpt.page ? `${title}, page ${excerpt.page}` : title;
}

/** Whether questions of the type can carry marking points: the ones a teacher (or AI) marks. */
export function usesScheme(type: QuestionType): boolean {
  return type === 'short_answer' || type === 'free_response';
}

/** 2–8 unique options. */
const optionsValidator: ValidatorFn = (control: AbstractControl<string>) => {
  const options = optionLines(control.value);
  return options.length >= 2 && options.length <= 8 && new Set(options).size === options.length
    ? null
    : { options: true };
};

/** 1–20 accepted answers. */
const acceptedValidator: ValidatorFn = (control: AbstractControl<string>) => {
  const accepted = optionLines(control.value);
  return accepted.length >= 1 && accepted.length <= MAX_ACCEPTED ? null : { accepted: true };
};

export function assessmentForm(): AssessmentForm {
  return new FormGroup({
    title: new FormControl('', { nonNullable: true, validators: requiredText(160) }),
    opens_at: new FormControl('', { nonNullable: true }),
    due_at: new FormControl('', { nonNullable: true }),
    late_policy: new FormControl<LatePolicy>('accept_flagged', { nonNullable: true }),
    release_policy: new FormControl<ReleasePolicy>('on_finalize', { nonNullable: true }),
    ai_marking: new FormControl(false, { nonNullable: true }),
    questions: new FormArray<QuestionForm>([questionForm()], [assessmentSchemeValidator]),
  });
}

/** Fills the editor with a draft to change. */
export function fillForm(form: AssessmentForm, draft: Assessment): void {
  form.controls.questions.clear();
  for (const q of draft.questions) form.controls.questions.push(questionForm(q));
  form.reset({
    title: draft.title,
    opens_at: localInput(draft.opens_at ?? null),
    due_at: localInput(draft.due_at ?? null),
    late_policy: draft.late_policy ?? 'accept_flagged',
    release_policy: draft.release_policy ?? 'on_finalize',
    ai_marking: draft.ai_marking ?? false,
  });
}

/** Empties the editor back to one blank question. */
export function clearForm(form: AssessmentForm): void {
  form.controls.questions.clear();
  form.controls.questions.push(questionForm());
  form.reset();
}

/** The assessment the editor describes, as a draft or to publish. */
export function toAssessment(
  form: AssessmentForm,
  classroomId: string,
  status: AssessmentSettings['status'],
): NewAssessment {
  const v = form.getRawValue();
  return {
    classroom_id: classroomId,
    title: v.title,
    status,
    opens_at: isoOrNull(v.opens_at),
    due_at: isoOrNull(v.due_at),
    late_policy: v.late_policy,
    release_policy: v.release_policy,
    // Only questions a teacher marks can have suggestions.
    ai_marking: v.ai_marking && teacherMarked(v.questions),
    questions: v.questions.map(toQuestion),
  };
}

/** The details the editor describes, which can change after students have submitted. */
export function toDetails(form: AssessmentForm): AssessmentDetails {
  const v = form.getRawValue();
  return { title: v.title, due_at: isoOrNull(v.due_at), late_policy: v.late_policy };
}

function toQuestion(q: ReturnType<QuestionForm['getRawValue']>): Omit<Question, 'id'> {
  const base = {
    prompt: q.prompt,
    type: q.type,
    topic_id: q.topic_id,
    options: [] as string[],
    ...(q.origin ? { origin: q.origin } : {}),
    ...(q.sources.length ? { sources: q.sources } : {}),
  };
  switch (q.type) {
    case 'multiple_choice':
      return { ...base, options: optionLines(q.options), answer: q.answer, tolerance: '', points: q.points };
    case 'numerical':
      return { ...base, answer: q.answer, tolerance: q.tolerance, points: q.points };
    case 'short_answer':
      return {
        ...base,
        answer: '',
        tolerance: '',
        accepted_answers: optionLines(q.accepted),
        case_sensitive: q.case_sensitive,
        points: q.points,
        ...scheme(q.marking_scheme),
      };
    case 'free_response':
      return {
        ...base,
        answer: '',
        tolerance: '',
        max_length: q.max_length,
        // Assessment sets a free response's points to its rubric's total.
        points: rubricTotal(q.criteria),
        rubric: {
          criteria: q.criteria.map((c) => ({ title: c.title, levels: parseLevels(c.levels) ?? [] })),
          ...(q.model_answer.trim() ? { model_answer: q.model_answer } : {}),
        },
        ...scheme(q.marking_scheme),
      };
  }
}

/** The marking_scheme field, left out when there are no passages. */
function scheme(excerpts: Excerpt[]): { marking_scheme?: Excerpt[] } {
  return excerpts.length ? { marking_scheme: excerpts } : {};
}

export function teacherMarked(questions: readonly { type: QuestionType }[]): boolean {
  return questions.some((q) => usesScheme(q.type));
}

/** A datetime-local value (the viewer's own time zone) as an ISO instant, or null when empty. */
export function isoOrNull(local: string): string | null {
  return local ? new Date(local).toISOString() : null;
}

/** An ISO instant as a datetime-local value in the viewer's own time zone. */
export function localInput(iso: string | null): string {
  if (!iso) return '';
  const at = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}T${pad(at.getHours())}:${pad(at.getMinutes())}`;
}

/** Where a student stands with an assessment's due date. */
export type DueState = 'none' | 'upcoming' | 'late' | 'closed';

export function dueState(
  due: string | null | undefined,
  latePolicy: LatePolicy | undefined,
  now = new Date(),
): DueState {
  if (!due) return 'none';
  if (new Date(due) >= now) return 'upcoming';
  return latePolicy === 'reject' ? 'closed' : 'late';
}
