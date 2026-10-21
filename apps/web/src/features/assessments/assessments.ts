import { messageOf } from '../../shared/errors';
import { PagedSection } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ReactiveFormsModule, type ValidatorFn } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { AssessmentApi, ContentApi } from '../../api/clients';
import type {
  Assessment,
  AssessmentSummary,
  DocumentRecord,
  Excerpt,
  Question,
  Topic,
} from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { dueState } from '../../shared/due';
import { Section, rereadAfterDelete } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import {
  MAX_ASSESSMENT_EXCERPTS,
  MAX_CRITERIA,
  MAX_QUESTIONS,
  assessmentForm,
  clearForm,
  criterionForm,
  excerptLabel,
  fillForm,
  markWrittenBy,
  questionForm,
  rubricTotal,
  schemeTotal,
  teacherMarked,
  toAssessment,
  toDetails,
  untouched,
  usesScheme,
  type QuestionForm,
} from './editor';
import { AnswerSheetComponent, schedule } from './answer-sheet';
import { QuestionGeneratorComponent } from './generator';
import { MarkingGuidanceComponent } from './guidance';
import { MarkingPointsComponent } from './marking-points';

/* why a published assessment's replacement was refused, after which the editor keeps only its details. */
const LOCKED =
  'Students have submitted this assessment, so only its title, due date and late policy can change now. Save ' +
  'again to keep those changes.';

@Component({
  selector: 'app-assessments',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
    NoClassroomComponent,
    PagerComponent,
    MarkingPointsComponent,
    QuestionGeneratorComponent,
    MarkingGuidanceComponent,
    AnswerSheetComponent,
  ],
  templateUrl: './assessments.html',
})
export class AssessmentsComponent {
  readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly contentApi = inject(ContentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  /* deleting an assessment, in its confirmation dialog. */
  readonly deleteState = new ActionState();
  readonly assessments = new PagedSection<AssessmentSummary>();
  readonly topics = this.curriculum.topics;
  /* reading the curriculum for the topic a new question uses. */
  readonly topicsState = new Section<Topic[]>();
  /* the classroom's documents, read once for generation, drafted guidance and where passages came from. */
  readonly classDocs = new Section<DocumentRecord[]>();
  /* the open assessment, answered or previewed. */
  @ViewChild(AnswerSheetComponent, { static: true }) sheet?: AnswerSheetComponent;
  /* the assessment the editor is changing; null while it creates a new one. */
  readonly editing = signal<Assessment | null>(null);
  /* whether anyone had submitted the assessment being edited when the teacher opened it. */
  readonly editingSubmitted = signal(false);
  /* whether the editor changes a published assessment, which stays published. */
  readonly editingPublished = computed(() => this.editing()?.status === 'published');
  /* submitted assessments allow only title, due-date and late-policy edits. */
  readonly detailsOnly = computed(() => this.editingPublished() && this.editingSubmitted());
  /* the assessment waiting for its deletion to be confirmed. */
  readonly deleting = signal<AssessmentSummary | null>(null);
  /* whether deleting the assessment waiting for confirmation also deletes submissions and their grades. */
  readonly deletingSubmitted = computed(() => {
    const item = this.deleting();
    return !!item && submittedBy(item);
  });
  readonly submittedBy = submittedBy;
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  readonly maxQuestions = MAX_QUESTIONS;
  readonly maxCriteria = MAX_CRITERIA;
  readonly maxMarkingPoints = MAX_ASSESSMENT_EXCERPTS;
  readonly usesScheme = usesScheme;
  /* the assessment a link such as student home asked to open, until the classroom's list has it. */
  private requested = '';
  readonly form = assessmentForm();
  /* re-read on every change to the form's questions, so the ai marking option knows whether it applies. */
  private readonly questionTypes = signal(this.form.controls.questions.getRawValue().map((q) => q.type));
  readonly canSuggest = computed(() => teacherMarked(this.questionTypes().map((type) => ({ type }))));

  constructor() {
    // the open assessment's answers are tracked by the answer sheet.
    inject(UnsavedChanges).track(() => this.form.dirty);
    this.form.controls.questions.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.typesChanged());
    this.typesChanged();
    onClassroomChange(() => {
      this.cancelEdit();
      this.assessments.page.set(0);
      this.load();
    });
    inject(ActivatedRoute)
      .queryParamMap.pipe(takeUntilDestroyed())
      .subscribe((params) => {
        this.requested = params.get('open') ?? '';
        this.openRequested();
      });
  }

  /* ai marking applies only to questions a teacher marks, so the option is off without one. */
  private typesChanged(): void {
    this.questionTypes.set(this.form.controls.questions.getRawValue().map((q) => q.type));
    const option = this.form.controls.ai_marking;
    if (this.canSuggest()) option.enable({ emitEvent: false });
    else option.disable({ emitEvent: false });
  }

  get questions() {
    return this.form.controls.questions;
  }

  /* the topic's name when it is archived, or '' for an active (or unknown) topic. */
  archivedTopic(topicId: string): string {
    return this.curriculum.archived().find((t) => t.id === topicId)?.name ?? '';
  }

  /* an archived topic is out of every picker for new questions, so a question that still has one must change it. */
  private readonly activeTopic: ValidatorFn = (control) =>
    this.archivedTopic(control.value as string) ? { archived: true } : null;

  load(): void {
    const classroomId = this.classroomId();
    this.assessments.clear();
    this.classDocs.clear();
    if (!classroomId) return;
    void this.assessments
      .load(() =>
        this.assessments.readPage((offset, limit) =>
          this.assessmentApi.assessmentPage(classroomId, offset, limit),
        ),
      )
      .then(() => this.openRequested());
    if (this.session.isTeacher()) {
      // read fresh, so a topic a colleague just added is there to choose.
      void this.topicsState.load(() => this.curriculum.refresh());
      void this.classDocs.load(() => this.contentApi.documents(classroomId));
    }
  }

  /* opens the requested assessment once it is in the current classroom's list. */
  private openRequested(): void {
    if (!this.requested || !this.classroomId() || this.assessments.loading()) return;
    const id = this.requested;
    const classroomId = this.classroomId();
    this.requested = '';
    if (this.assessments.value()?.some((a) => a.id === id)) {
      this.sheet?.open(id);
      return;
    }
    // a home/deep link can name an assessment on another page; the server still authorizes the read.
    void this.assessmentApi
      .get(id)
      .then((item) => {
        if (this.classroomId() === classroomId && item.classroom_id === classroomId) this.sheet?.open(id);
      })
      .catch((error: unknown) =>
        this.state.error.set(messageOf(error, 'The assessment could not be opened.')),
      );
  }

  // --- the teacher's editor
  // --------------------------------------------------------------------------------

  addQuestion(): void {
    if (this.questions.length >= MAX_QUESTIONS) return;
    // a new question starts with the previous one's topic, which is usually the same, unless it is
    // archived.
    const topic = this.questions.at(this.questions.length - 1)?.controls.topic_id.value ?? '';
    const q = questionForm();
    q.controls.topic_id.setValue(this.archivedTopic(topic) ? '' : topic);
    this.questions.push(q);
    this.form.markAsDirty();
    this.focusLater(`question-${this.questions.length - 1}-prompt`);
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
    return excerptLabel(excerpt, this.classDocs.value());
  }

  removeQuestion(index: number): void {
    if (this.questions.length <= 1) return;
    this.questions.removeAt(index);
    this.form.markAsDirty();
    this.focusLater(`question-${Math.min(index, this.questions.length - 1)}-prompt`);
  }

  addCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length >= MAX_CRITERIA) return;
    question.controls.criteria.push(criterionForm());
    this.form.markAsDirty();
    this.focusLater(`question-${index}-criterion-${question.controls.criteria.length - 1}-title`);
  }

  removeCriterion(question: QuestionForm, index: number): void {
    if (question.controls.criteria.length <= 1) return;
    question.controls.criteria.removeAt(index);
    this.form.markAsDirty();
  }

  /* a free response's points: its rubric's highest levels added up. */
  rubricPoints(question: QuestionForm): number {
    return rubricTotal(question.controls.criteria.getRawValue());
  }

  private focusLater(id: string): void {
    setTimeout(() => document.getElementById(id)?.focus());
  }

  /* saves the editor as a draft (new or existing), publishes it, or saves the changes to a published assessment (only its details once students have submitted it). */
  save(publish: boolean): void {
    if (!this.state.validate(this.form, 'assessment-editor')) return;
    const editing = this.editing();
    if (editing && this.editingPublished()) {
      this.saveChanges(editing);
      return;
    }
    const status = publish ? 'published' : 'draft';
    const classroomId = this.classroomId();
    const assessment = toAssessment(this.form, classroomId, status);
    const message = publish ? 'Assessment published.' : editing ? 'Draft saved.' : 'Draft created.';
    void this.state.run(async () => {
      const saved = editing
        ? await this.assessmentApi.update(editing.id, assessment)
        : await this.assessmentApi.create(assessment);
      if (publish) this.cancelEdit();
      else {
        this.editing.set(saved);
        this.form.markAsPristine();
      }
      await this.assessments.reload(() =>
        this.assessments.readPage((offset, limit) =>
          this.assessmentApi.assessmentPage(classroomId, offset, limit),
        ),
      );
    }, message);
  }

  /* saves the editor's changes to a published assessment. */
  private saveChanges(editing: Assessment): void {
    const classroomId = this.classroomId();
    const detailsOnly = this.detailsOnly();
    void this.state.run(async () => {
      try {
        if (detailsOnly) await this.assessmentApi.details(editing.id, toDetails(this.form));
        else await this.assessmentApi.update(editing.id, toAssessment(this.form, classroomId, 'published'));
      } catch (error) {
        const refused = hasSubmissions(error);
        await this.assessments
          .reload(() =>
            this.assessments.readPage((offset, limit) =>
              this.assessmentApi.assessmentPage(classroomId, offset, limit),
            ),
          )
          .catch(() => undefined);
        const now = this.assessments.value()?.find((a) => a.id === editing.id);
        if (this.editing()?.id === editing.id && (refused || (now && submittedBy(now)))) this.lockTo(true);
        throw refused ? new Error(LOCKED) : error;
      }
      this.cancelEdit();
      await this.assessments.reload(() =>
        this.assessments.readPage((offset, limit) =>
          this.assessmentApi.assessmentPage(classroomId, offset, limit),
        ),
      );
    }, `${this.form.controls.title.value} saved.`);
  }

  edit(item: AssessmentSummary): void {
    if (this.form.dirty) {
      this.state.error.set('Save or clear the assessment you are editing first.');
      return;
    }
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      const assessment = await this.assessmentApi.get(item.id);
      // know which of its topics are archived; without the curriculum, the server still refuses one.
      await this.curriculum.need(assessment.questions.map((q) => q.topic_id)).catch(() => undefined);
      // the user may have moved to another classroom while the assessment loaded.
      if (this.classroomId() !== classroomId) return;
      // whole again, in case the last assessment edited was locked to its details.
      this.form.enable({ emitEvent: false });
      fillForm(this.form, assessment);
      for (const q of this.questions.controls) {
        const topic = q.controls.topic_id;
        topic.addValidators(this.activeTopic);
        topic.updateValueAndValidity({ emitEvent: false });
        // say at once which questions need another topic.
        if (topic.invalid) topic.markAsTouched();
      }
      this.editing.set(assessment);
      this.typesChanged();
      this.lockTo(submittedBy(item));
      this.focusLater('assessment-title');
    });
  }

  /* records whether anyone has submitted the assessment being edited and, once anyone has, disables everything but its details, so the form checks and sends only those. */
  private lockTo(submitted: boolean): void {
    this.editingSubmitted.set(submitted);
    if (!this.detailsOnly()) return;
    const c = this.form.controls;
    for (const control of [c.questions, c.opens_at, c.release_policy, c.ai_marking])
      control.disable({ emitEvent: false });
  }

  cancelEdit(): void {
    this.form.enable({ emitEvent: false });
    clearForm(this.form);
    this.editing.set(null);
    this.editingSubmitted.set(false);
    this.typesChanged();
  }

  publish(item: AssessmentSummary): void {
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      await this.assessmentApi.publish(item.id);
      if (this.editing()?.id === item.id) this.cancelEdit();
      await this.assessments.reload(() =>
        this.assessments.readPage((offset, limit) =>
          this.assessmentApi.assessmentPage(classroomId, offset, limit),
        ),
      );
    }, `${item.title} published.`);
  }

  confirmDelete(item: AssessmentSummary): void {
    this.deleting.set(item);
    this.deleteDialog?.show();
  }

  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.close();
  }

  remove(): void {
    const item = this.deleting();
    const classroomId = this.classroomId();
    if (!item) return;
    void this.deleteState.run(async () => {
      try {
        await this.assessmentApi.remove(item.id, submittedBy(item));
      } catch (error) {
        // a student may have submitted since the list was read: the dialog then says what deleting now
        // takes.
        const refused = hasSubmissions(error);
        const reread = await this.assessments
          .reload(() =>
            this.assessments.readPage((offset, limit) =>
              this.assessmentApi.assessmentPage(classroomId, offset, limit),
            ),
          )
          .then(
            () => true,
            () => false,
          );
        const now = (reread && this.assessments.value()?.find((a) => a.id === item.id)) || item;
        // without a fresh count, who submitted is unknown: the dialog says so rather than guessing.
        const counted = reread ? now : { ...now, submissions: undefined };
        if (this.deleting()?.id === item.id)
          this.deleting.set(refused ? { ...counted, has_submissions: true } : now);
        throw error;
      }
      if (this.editing()?.id === item.id) this.cancelEdit();
      if (this.sheet?.selected()?.id === item.id) this.sheet.close();
      this.closeDelete();
      this.state.error.set('');
      this.state.message.set(item.status === 'draft' ? 'Draft deleted.' : `${item.title} deleted.`);
      // the dialog is closed, so a failure is reported on the page, without the deleted assessment.
      await rereadAfterDelete(
        this.assessments,
        item.id,
        () =>
          this.assessments.reload(() =>
            this.assessments.readPage((offset, limit) =>
              this.assessmentApi.assessmentPage(classroomId, offset, limit),
            ),
          ),
        () => this.classroomId() === classroomId,
        this.state,
        { list: 'list', refresh: 'Refresh assessments' },
      );
    });
  }

  // --- labels
  // ----------------------------------------------------------------------------------------------

  readonly schedule = schedule;

  dueBadge(item: AssessmentSummary): string {
    if (item.status === 'draft' || item.submitted) return '';
    const state = dueState(item.due_at, item.late_policy);
    return state === 'late' ? 'Overdue' : state === 'closed' ? 'Closed' : '';
  }
}

/* whether anyone has submitted the assessment, students since removed from the classroom included. */
function submittedBy(item: AssessmentSummary): boolean {
  return item.has_submissions ?? !!item.submissions;
}

/* whether the server refused a change because students have submitted the assessment. */
function hasSubmissions(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'HAS_SUBMISSIONS';
}
