import { Component, EventEmitter, Input, OnChanges, Output, computed, inject, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, BankQuestion, Evaluation, Question, QuestionType, Student } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ActionState } from '../../shared/action-state';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { studentName, total } from '../../shared/names';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagedSection } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { BANK_TYPES } from './bank-picker';
import { marksText } from './editor';
import {
  type FinalizedGrades,
  RESIT_QUESTIONS,
  RESIT_STUDENTS,
  finalizedGrades,
  resitRefusal,
  resitRequest,
  strugglingStudents,
  suggestedQuestions,
  weakTopics,
} from './resit';

/* the resit builder (`/assessments?resit=<id>`, adr 0017): students and topics suggested from the original's finalized grades, then its questions and the bank's, created as a draft for the teacher to review and publish. */
@Component({
  selector: 'app-resit-builder',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    RouterLink,
    FieldComponent,
    FieldControlDirective,
    IconComponent,
    PageHeaderComponent,
    PagerComponent,
    RefreshButtonComponent,
    SectionStateComponent,
    StatusComponent,
  ],
  templateUrl: './resit-builder.html',
})
export class ResitBuilderComponent implements OnChanges {
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly gradingApi = inject(GradingApi);
  private readonly identityApi = inject(IdentityApi);
  private readonly curriculum = inject(CurriculumNames);
  /* the published assessment to resit, from the address; '' while the view is not shown. */
  @Input() assessmentId = '';
  @Input() classroomId = '';
  @Input() shown = false;
  /* the new draft, which the page opens in the editor. */
  @Output() readonly created = new EventEmitter<Assessment>();

  readonly maxStudents = RESIT_STUDENTS;
  readonly maxQuestions = RESIT_QUESTIONS;
  readonly marksText = marksText;
  readonly state = new ActionState();
  readonly original = new Section<Assessment>();
  readonly grades = new Section<FinalizedGrades>();
  readonly roster = new Section<Student[]>();
  readonly bank = new PagedSection<BankQuestion>();
  readonly students = signal<ReadonlySet<string>>(new Set());
  readonly copies = signal<ReadonlySet<string>>(new Set());
  /* the bank questions chosen, in the order chosen, kept across topics and pages. */
  readonly banked = signal<ReadonlyMap<string, BankQuestion>>(new Map());
  readonly title = new FormControl('', { nonNullable: true, validators: Validators.maxLength(160) });
  readonly topic = new FormControl('', { nonNullable: true });
  readonly hideAnswered = new FormControl(false, { nonNullable: true });
  readonly form = new FormGroup({ title: this.title, topic: this.topic, hideAnswered: this.hideAnswered });
  /* the suggestions stop following the grades once the teacher ticks a box themselves. */
  private studentsTouched = false;
  private questionsTouched = false;
  /* the bank's topic follows the weak topics until the teacher picks one. */
  private topicTouched = false;
  /* the assessment and classroom read, and the bank filter last read. */
  private opened = '';
  private bankKey = '';

  readonly questions = computed<Question[]>(() => this.original.value()?.questions ?? []);
  private readonly finalized = computed<Evaluation[]>(() => this.grades.value()?.grades ?? []);
  /* students who lost marks on a finalized grade. */
  readonly struggling = computed(() => new Set(strugglingStudents(this.finalized())));
  /* the original's questions the chosen students lost marks on. */
  readonly suggested = computed(() => suggestedQuestions(this.finalized(), this.students()));
  readonly topics = computed(() =>
    weakTopics(this.finalized(), this.students(), this.questions(), this.copies()),
  );
  /* the one student chosen, whose answered bank questions are marked; '' for none or several. */
  readonly single = computed(() => (this.students().size === 1 ? [...this.students()][0] : ''));
  readonly bankedList = computed(() => [...this.banked().values()]);
  readonly questionCount = computed(() => this.copies().size + this.banked().size);
  readonly studentsFull = computed(() => this.students().size >= RESIT_STUDENTS);
  readonly questionsFull = computed(() => this.questionCount() >= RESIT_QUESTIONS);

  constructor() {
    this.topic.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => {
      this.topicTouched = true;
      this.refreshBank();
    });
    this.hideAnswered.valueChanges.pipe(takeUntilDestroyed()).subscribe(() => this.refreshBank());
  }

  ngOnChanges(): void {
    const key = this.assessmentId && this.classroomId ? `${this.assessmentId}|${this.classroomId}` : '';
    if (key === this.opened) return;
    this.opened = key;
    this.reset();
    if (key) this.load();
  }

  heading(): string {
    const assessment = this.original.value();
    return assessment ? 'Create a resit: ' + assessment.title : 'Create a resit';
  }

  /* what the server names the resit when the title is left empty. */
  defaultTitle(): string {
    return `${this.original.value()?.title ?? ''} (resit)`;
  }

  // --- reading
  // -------------------------------------------------------------------------------------------

  private reset(): void {
    this.original.clear();
    this.grades.clear();
    this.roster.clear();
    this.bank.clear();
    this.bank.page.set(0);
    this.students.set(new Set());
    this.copies.set(new Set());
    this.banked.set(new Map());
    this.form.reset({ title: '', topic: '', hideAnswered: false }, { emitEvent: false });
    this.state.error.set('');
    this.state.message.set('');
    this.studentsTouched = false;
    this.questionsTouched = false;
    this.topicTouched = false;
    this.bankKey = '';
  }

  /* reads the original, then its grades and the roster beside each other; choices already made are kept. */
  load(): void {
    const { assessmentId, classroomId } = this;
    const current = () => this.assessmentId === assessmentId && this.classroomId === classroomId;
    void this.original
      .load(async () => {
        const assessment = await this.assessmentApi.editable(assessmentId);
        if (assessment.classroom_id !== classroomId)
          throw new Error('This assessment is not in the current classroom.');
        if (assessment.status !== 'published')
          throw new Error('Only a published assessment can have a resit. Publish it first.');
        // name every topic its questions use, archived ones included.
        await this.curriculum.need(assessment.questions.map((q) => q.topic_id)).catch(() => undefined);
        return assessment;
      })
      .then(() => {
        if (!current() || !this.original.value()) return;
        const ids = new Set(this.questions().map((q) => q.id));
        this.copies.update((copies) => new Set([...copies].filter((id) => ids.has(id))));
        this.bankKey = '';
        this.loadGrades();
        this.loadRoster();
      });
  }

  loadGrades(): void {
    const { assessmentId, classroomId } = this;
    void this.grades
      .load(() =>
        finalizedGrades((offset, limit) =>
          this.gradingApi.evaluations(classroomId, assessmentId, offset, limit),
        ),
      )
      .then(() => this.suggest());
  }

  loadRoster(): void {
    const { classroomId } = this;
    void this.roster
      .load(() => this.identityApi.roster(classroomId))
      .then(() => {
        // a chosen student no longer on the roster could not be unticked.
        const ids = new Set((this.roster.value() ?? []).map((s) => s.id));
        if (this.roster.value()) this.students.update((s) => new Set([...s].filter((id) => ids.has(id))));
        this.suggest();
      });
  }

  /* the header's refresh: every part again, keeping the choices. */
  refresh(): void {
    this.load();
  }

  /* ticks the suggested students and questions until the teacher has changed either. */
  private suggest(): void {
    const roster = this.roster.value();
    if (!this.studentsTouched && roster && this.grades.value()) {
      const enrolled = new Set(roster.map((s) => s.id));
      const struggling = strugglingStudents(this.finalized()).filter((id) => enrolled.has(id));
      this.students.set(new Set(struggling.slice(0, RESIT_STUDENTS)));
    }
    this.followStudents();
  }

  /* the question ticks and the bank's topic follow the chosen students. */
  private followStudents(): void {
    if (!this.questionsTouched) {
      const ids = new Set(this.questions().map((q) => q.id));
      const room = RESIT_QUESTIONS - this.banked().size;
      this.copies.set(new Set([...this.suggested()].filter((id) => ids.has(id)).slice(0, room)));
    }
    const topics = this.topics();
    // '' is every topic, which the teacher can always pick.
    const kept = this.topicTouched && (!this.topic.value || topics.includes(this.topic.value));
    if (!kept) this.topic.setValue(topics[0] ?? '', { emitEvent: false });
    this.refreshBank();
  }

  /* reads the bank again when its filter changed: the topic, the one student chosen, or hiding answered ones. */
  private refreshBank(): void {
    if (!this.original.value()) return;
    const single = this.single();
    const key = [this.topic.value, single, single && this.hideAnswered.value].join('|');
    if (key === this.bankKey) return;
    this.bankKey = key;
    this.bank.page.set(0);
    this.loadBank();
  }

  loadBank(): void {
    const single = this.single();
    const filter = {
      topicId: this.topic.value,
      classroomId: this.classroomId,
      studentId: single,
      excludeAnswered: !!single && this.hideAnswered.value,
    };
    void this.bank.load(() =>
      this.bank.readPage((offset, limit) => this.assessmentApi.bankPage(filter, offset, limit)),
    );
  }

  // --- choosing
  // -------------------------------------------------------------------------------------------

  toggleStudent(id: string, checked: boolean): void {
    if (checked && !this.students().has(id) && this.studentsFull()) return;
    this.studentsTouched = true;
    this.students.update((s) => toggled(s, id, checked));
    this.followStudents();
  }

  toggleCopy(id: string, checked: boolean): void {
    if (checked && !this.copies().has(id) && this.questionsFull()) return;
    this.questionsTouched = true;
    this.copies.update((c) => toggled(c, id, checked));
  }

  toggleBank(question: BankQuestion, checked: boolean): void {
    const banked = this.banked();
    if (checked && (banked.has(question.id) || this.questionsFull() || this.archived(question))) return;
    const next = new Map(banked);
    if (checked) next.set(question.id, question);
    else next.delete(question.id);
    this.banked.set(next);
  }

  // --- labels
  // -------------------------------------------------------------------------------------------

  studentName(student: Student): string {
    return studentName(student) || student.id;
  }

  /* a student's finalized marks on the original, or '' without a finalized grade. */
  marksOf(studentId: string): string {
    const grade = this.finalized().find((g) => g.student_id === studentId);
    if (!grade) return '';
    const { awarded, possible } = total(grade.evidence);
    return `${awarded} of ${marksText(possible)}`;
  }

  topicName(topicId: string): string {
    return this.curriculum.topicName(topicId) || topicId;
  }

  typeName(type: QuestionType): string {
    return BANK_TYPES.find((t) => t.value === type)?.label ?? type;
  }

  /* a bank question on an archived topic cannot go into an assessment. */
  archived(question: Pick<BankQuestion, 'topic_id'>): boolean {
    return this.curriculum.archived().some((t) => t.id === question.topic_id);
  }

  chosenStudentName(): string {
    const id = this.single();
    const student = this.roster.value()?.find((s) => s.id === id);
    return student ? this.studentName(student) : 'this student';
  }

  // --- creating
  // -------------------------------------------------------------------------------------------

  create(): void {
    const assessment = this.original.value();
    if (!assessment || this.state.busy()) return;
    this.state.message.set('');
    if (!this.students().size) {
      this.refuse('Choose at least one student for the resit.', 'resit-students');
      return;
    }
    if (!this.questionCount()) {
      this.refuse('Choose at least one question for the resit.', 'resit-questions');
      return;
    }
    const request = resitRequest(
      this.students(),
      assessment.questions,
      this.copies(),
      this.banked().keys(),
      this.title.value,
    );
    const count = request.student_ids.length;
    void this.state.run(
      async () => {
        const saved = await this.assessmentApi.createResit(assessment.id, request).catch((error: unknown) => {
          throw new Error(resitRefusal(error));
        });
        this.created.emit(saved);
      },
      `Resit draft created for ${count} ${count === 1 ? 'student' : 'students'}.`,
    );
  }

  /* says what is missing and moves focus to the first control of its part. */
  private refuse(message: string, partId: string): void {
    this.state.error.set(message);
    setTimeout(() =>
      document.getElementById(partId)?.querySelector<HTMLElement>('input:not(:disabled), select')?.focus(),
    );
  }
}

function toggled(set: ReadonlySet<string>, id: string, checked: boolean): Set<string> {
  const next = new Set(set);
  if (checked) next.add(id);
  else next.delete(id);
  return next;
}
