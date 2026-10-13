import { Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { AssessmentApi, CurriculumApi, NewAssessment } from '../../api/clients';
import type { Assessment, Node, Question } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { StatusComponent } from '../../shared/status';

type QuestionType = Question['type'];

@Component({
  selector: 'app-assessments',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './assessments.html',
})
export class AssessmentsComponent {
  readonly session = inject(Session);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly assessments = signal<Pick<Assessment, 'id' | 'title'>[]>([]);
  readonly skills = signal<Node[]>([]);
  readonly selected = signal<Assessment | null>(null);
  readonly submitted = signal(false);
  answers = new FormGroup<Record<string, FormControl<string>>>({});
  readonly form = new FormGroup({
    title: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(160)],
    }),
    prompt: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(2000)],
    }),
    skill_id: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    type: new FormControl<QuestionType>('numerical', { nonNullable: true }),
    options: new FormControl('', { nonNullable: true }),
    answer: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    tolerance: new FormControl('0', { nonNullable: true }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.answers.dirty);
    onClassroomChange(() => {
      this.selected.set(null);
      this.assessments.set([]);
      this.load();
    });
  }

  load(): void {
    const classroomId = this.classroomId();
    if (!classroomId) return;
    void this.state.run(async () => {
      this.assessments.set(await this.assessmentApi.list(classroomId));
      this.skills.set((await this.curriculum.nodes()).filter((n) => n.kind === 'skill'));
    });
  }

  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.assessmentApi.create(this.newAssessment());
      this.form.reset({ type: 'numerical', tolerance: '0' });
      this.assessments.set(await this.assessmentApi.list(this.classroomId()));
    }, 'Assessment created.');
  }

  /** The one-question assessment the form describes. */
  private newAssessment(): NewAssessment {
    const v = this.form.getRawValue();
    const multipleChoice = v.type === 'multiple_choice';
    return {
      classroom_id: this.classroomId(),
      title: v.title,
      questions: [
        {
          prompt: v.prompt,
          skill_id: v.skill_id,
          type: v.type,
          answer: v.answer,
          options: multipleChoice ? optionLines(v.options) : [],
          tolerance: multipleChoice ? '' : v.tolerance,
        },
      ],
    };
  }

  open(id: string): void {
    if (this.answers.dirty) {
      this.state.error.set('Submit your current answers before opening another assessment.');
      return;
    }
    void this.state.run(async () => {
      const assessment = await this.assessmentApi.get(id);
      this.answers = new FormGroup(
        Object.fromEntries(
          assessment.questions.map((q) => [
            q.id!,
            new FormControl('', { nonNullable: true, validators: [Validators.required] }),
          ]),
        ),
      );
      this.selected.set(assessment);
      this.submitted.set(false);
    });
  }

  submit(): void {
    const assessment = this.selected();
    if (!assessment || !this.state.validate(this.answers)) return;
    void this.state.run(async () => {
      await this.assessmentApi.submit(assessment.id, this.answers.getRawValue());
      this.answers.markAsPristine();
      this.submitted.set(true);
    }, 'Answers submitted. Your teacher will review the grade.');
  }
}

/** Multiple-choice options, one per non-blank line. */
export function optionLines(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean);
}
