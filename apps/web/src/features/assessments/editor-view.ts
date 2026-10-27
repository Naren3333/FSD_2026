import { Component, EventEmitter, Input, Output, inject, signal } from '@angular/core';
import { FormArray, FormGroup, ReactiveFormsModule, type AbstractControl } from '@angular/forms';
import { RouterLink } from '@angular/router';
import type { Assessment, DocumentRecord, Excerpt, Question, Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ActionState } from '../../shared/action-state';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { PageHeaderComponent } from '../../shared/page-header';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import {
  MAX_ASSESSMENT_EXCERPTS,
  MAX_CRITERIA,
  MAX_QUESTIONS,
  criterionForm,
  excerptLabel,
  markWrittenBy,
  marksText,
  parseLevels,
  questionForm,
  rubricTotal,
  schemeTotal,
  untouched,
  usesScheme,
  type AssessmentForm,
  type QuestionForm,
} from './editor';
import { QuestionGeneratorComponent } from './generator';
import { MarkingGuidanceComponent } from './guidance';
import { MarkingPointsComponent } from './marking-points';
import { focusLater } from '../../shared/focus';
import { dateTime } from '../../shared/format';

/* the teacher's editor: an assessment's questions, schedule and grading, with its outline, ai drafting panel and save bar. */
@Component({
  selector: 'app-assessment-editor',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RouterLink,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    IconComponent,
    StatusComponent,
    SectionStateComponent,
    MarkingPointsComponent,
    QuestionGeneratorComponent,
    MarkingGuidanceComponent,
  ],
  templateUrl: './editor-view.html',
})
export class AssessmentEditorComponent {
  private readonly curriculum = inject(CurriculumNames);
  @Input({ required: true }) form!: AssessmentForm;
  @Input({ required: true }) state!: ActionState;
  @Input({ required: true }) classroomId = '';
  @Input({ required: true }) documents!: Section<DocumentRecord[]>;
  /* reading the curriculum for the topic a new question uses. */
  @Input({ required: true }) topicsState!: Section<Topic[]>;
  /* the assessment being changed; null while creating a new one. */
  @Input() editing: Assessment | null = null;
  @Input() detailsOnly = false;
  @Input() editingPublished = false;
  @Input() canSuggest = false;
  /* whether the editor is the teacher's view, and whether the page's status shows in its save bar. */
  @Input() shown = false;
  @Input() showStatus = false;
  /* save as a draft (false), or publish (true); a published assessment saves its changes either way. */
  @Output() readonly save = new EventEmitter<boolean>();
  /* stop editing, or clear a new assessment's form. */
  @Output() readonly cancel = new EventEmitter<void>();

  readonly topics = this.curriculum.topics;
  readonly maxQuestions = MAX_QUESTIONS;
  readonly maxCriteria = MAX_CRITERIA;
  readonly maxMarkingPoints = MAX_ASSESSMENT_EXCERPTS;
  readonly usesScheme = usesScheme;
  readonly marksText = marksText;
  /* a criterion's levels as the editor reads them, or null when a line cannot be read. */
  readonly parseLevels = parseLevels;

  get questions() {
    return this.form.controls.questions;
  }

  /* the topic's name when it is archived, or '' for an active (or unknown) topic. */
  archivedTopic(topicId: string): string {
    return this.curriculum.archived().find((t) => t.id === topicId)?.name ?? '';
  }

  addQuestion(): void {
    if (this.questions.length >= MAX_QUESTIONS) return;
    // a new question starts with the previous one's topic, which is usually the same, unless it is
    // archived.
    const topic = this.questions.at(this.questions.length - 1)?.controls.topic_id.value ?? '';
    const q = questionForm();
    q.controls.topic_id.setValue(this.archivedTopic(topic) ? '' : topic);
    this.questions.push(q);
    this.form.markAsDirty();
    this.focusOn(q);
    this.revealAndFocus(`question-${this.questions.length - 1}-prompt`);
  }

  /* adds a generated draft the teacher kept, in place of the editor's one question if the teacher has not started it. */
  addGenerated(question: Omit<Question, 'id'>): void {
    const replace = this.questions.length === 1 && untouched(this.questions.at(0));
    if (!replace && this.questions.length >= MAX_QUESTIONS) return;
    const room = MAX_ASSESSMENT_EXCERPTS - (replace ? 0 : this.markingPoints());
    const scheme = question.marking_scheme ?? [];
    const kept =
      usesScheme(question.type) && scheme.length > room ? { ...question, marking_scheme: [] } : question;
    const form = questionForm(kept);
    for (const excerpt of kept.marking_scheme ?? [])
      markWrittenBy(form.controls.marking_scheme, excerpt, 'ai');
    if (replace) this.questions.setControl(0, form);
    else this.questions.push(form);
    this.form.markAsDirty();
    const position = replace ? 1 : this.questions.length;
    this.state.message.set(
      `Generated question added as question ${position}.` +
        (kept === question
          ? ''
          : ` Its marking points were left out: the assessment has ${MAX_ASSESSMENT_EXCERPTS} marking points already.`),
    );
  }

  /* the editor's question prompts, so generation leaves out near-duplicates. */
  prompts(): string[] {
    return this.questions.controls.map((q) => q.controls.prompt.value);
  }

  /* marking points across the editor's questions. */
  markingPoints(): number {
    return schemeTotal(this.questions.getRawValue());
  }

  /* where a generated question's passage is from. */
  sourceOf(excerpt: Excerpt): string {
    return excerptLabel(excerpt, this.documents.value());
  }

  removeQuestion(index: number): void {
    if (this.questions.length <= 1) return;
    this.questions.removeAt(index);
    this.form.markAsDirty();
    this.revealAndFocus(`question-${Math.min(index, this.questions.length - 1)}-prompt`);
  }

  addCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length >= MAX_CRITERIA) return;
    question.controls.criteria.push(criterionForm());
    this.form.markAsDirty();
    this.revealAndFocus(`question-${index}-criterion-${question.controls.criteria.length - 1}-title`);
  }

  removeCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length <= 1) return;
    question.controls.criteria.removeAt(index);
    this.form.markAsDirty();
  }

  /* the due date for the save bar, so it can be checked before publishing without scrolling to it. */
  dueLine(): string {
    const due = this.form.controls.due_at.value;
    // a value the browser could not read as a date counts as none, rather than breaking the bar.
    const when = due ? new Date(due) : null;
    return when && !Number.isNaN(when.getTime()) ? `Due ${dateTime(when.toISOString())}` : 'No due date';
  }

  /* the editor's heading, which the page header shows. */
  editorTitle(): string {
    const current = this.editing;
    if (!current) return 'Create an assessment';
    return (this.editingPublished ? 'Edit assessment: ' : 'Edit draft: ') + current.title;
  }

  /* a free response's points: its rubric's highest levels added up. */
  rubricPoints(question: QuestionForm): number {
    return rubricTotal(question.controls.criteria.getRawValue());
  }

  /* a question's marks: its own, or for a written response its rubric's. */
  marksOf(question: QuestionForm): number {
    return question.controls.type.value === 'free_response'
      ? this.rubricPoints(question)
      : Number(question.controls.points.value) || 0;
  }

  /* "3 questions · 8 marks", for the save bar. */
  size(): string {
    const count = this.questions.length;
    const marks = this.questions.controls.reduce((sum, q) => sum + this.marksOf(q), 0);
    return `${count} ${count === 1 ? 'question' : 'questions'} · ${marksText(marks)}`;
  }

  /* the outline's line for a question: the start of its prompt. */
  promptLine(question: QuestionForm): string {
    const prompt = question.controls.prompt.value.trim().split('\n')[0] ?? '';
    if (!prompt) return 'Untitled question';
    return prompt.length > 60 ? prompt.slice(0, 57).trimEnd() + '…' : prompt;
  }

  typeName(type: string): string {
    return TYPE_NAMES[type] ?? type;
  }

  /* a question's topic name, archived ones included, or ''. */
  topicName(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  /* whether the teacher has touched a control of the question that is invalid, for the outline's error dot. */
  hasErrors(question: QuestionForm): boolean {
    return invalidControl(question, true);
  }

  /* questions shown as their header only; collapsing keeps everything typed. */
  readonly collapsed = signal<ReadonlySet<QuestionForm>>(new Set());

  isCollapsed(question: QuestionForm): boolean {
    return this.collapsed().has(question);
  }

  toggleCollapsed(question: QuestionForm): void {
    const next = new Set(this.collapsed());
    if (!next.delete(question)) next.add(question);
    this.collapsed.set(next);
  }

  /* opens one question and folds the others, except any with an error, so a long assessment stays one screen per question. */
  focusOn(question: QuestionForm): void {
    this.collapsed.set(
      new Set(this.questions.controls.filter((q) => q !== question && !invalidControl(q, false))),
    );
  }

  /* opens every question with an error, so validation can focus the first one it sees. */
  expandInvalid(): void {
    this.expand(this.questions.controls.filter((q) => invalidControl(q, false)));
  }

  private expand(questions: readonly QuestionForm[]): void {
    const next = new Set(this.collapsed());
    for (const q of questions) next.delete(q);
    this.collapsed.set(next);
  }

  /* the outline's row: the question, opened, with its prompt focused. */
  goToQuestion(index: number): void {
    const question = this.questions.at(index);
    if (!question) return;
    this.focusOn(question);
    setTimeout(() => {
      document.getElementById(`question-${index}`)?.scrollIntoView({ block: 'start' });
      document.getElementById(`question-${index}-prompt`)?.focus({ preventScroll: true });
    });
  }

  /* the outline's last row: the schedule and grading settings, with their first control focused. */
  goToSchedule(): void {
    setTimeout(() => {
      document.getElementById('schedule-grading')?.scrollIntoView({ block: 'start' });
      document
        .getElementById('schedule-grading')
        ?.querySelector<HTMLElement>('input, select')
        ?.focus({ preventScroll: true });
    });
  }

  /* the ai drafting panel: closed means hidden, so its drafts and a generation on its way stay. */
  readonly generatorOpen = signal(false);

  toggleGenerator(): void {
    if (this.generatorOpen()) {
      this.closeGenerator();
      return;
    }
    this.generatorOpen.set(true);
    this.revealAndFocus('generator-heading');
  }

  closeGenerator(): void {
    this.generatorOpen.set(false);
    this.revealAndFocus('generator-toggle');
  }

  private revealAndFocus(id: string): void {
    // a question focused this way is opened first.
    const question = /^question-(\d+)-/.exec(id);
    if (question) {
      const q = this.questions.at(Number(question[1]));
      if (q) this.expand([q]);
    }
    focusLater(id);
  }
}

const TYPE_NAMES: Record<string, string> = {
  numerical: 'Numerical',
  multiple_choice: 'Multiple choice',
  short_answer: 'Short answer',
  free_response: 'Written response',
};

/* whether an enabled control in `control` is invalid (and, with `touched`, touched). */
function invalidControl(control: AbstractControl, touched: boolean): boolean {
  if (control.disabled) return false;
  if (control instanceof FormGroup || control instanceof FormArray)
    return Object.values(control.controls as Record<string, AbstractControl>).some((c) =>
      invalidControl(c, touched),
    );
  return control.invalid && (!touched || control.touched);
}
