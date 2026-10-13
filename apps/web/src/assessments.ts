import { Component, Input, OnChanges, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, Assessment, Node } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';
@Component({
  selector: 'app-assessments',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './assessments.html',
})
export class AssessmentsComponent implements OnChanges {
  readonly api = inject(Api);
  readonly state = new ActionState();
  @Input() classroomId = '';
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
    type: new FormControl<'numerical' | 'multiple_choice'>('numerical', { nonNullable: true }),
    options: new FormControl('', { nonNullable: true }),
    answer: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
    tolerance: new FormControl('0', { nonNullable: true }),
  });
  ngOnChanges(): void {
    this.selected.set(null);
    this.assessments.set([]);
    this.load();
  }
  load(): void {
    if (!this.classroomId) return;
    void this.state.run(async () => {
      this.assessments.set(await this.api.assessments(this.classroomId));
      this.skills.set((await this.api.nodes()).filter((n) => n.kind === 'skill'));
    });
  }
  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      const v = this.form.getRawValue();
      await this.api.request('assessment', '/assessments', 'POST', {
        classroom_id: this.classroomId,
        title: v.title,
        questions: [
          {
            prompt: v.prompt,
            skill_id: v.skill_id,
            type: v.type,
            answer: v.answer,
            options:
              v.type === 'multiple_choice'
                ? v.options
                    .split('\n')
                    .map((s) => s.trim())
                    .filter(Boolean)
                : [],
            tolerance: v.type === 'numerical' ? v.tolerance : '',
          },
        ],
      });
      this.form.reset({ type: 'numerical', tolerance: '0' });
      this.assessments.set(await this.api.assessments(this.classroomId));
    }, 'Assessment created.');
  }
  open(id: string): void {
    if (this.answers.dirty) {
      this.state.error.set('Submit your current answers before opening another assessment.');
      return;
    }
    void this.state.run(async () => {
      const assessment = await this.api.request<Assessment>('assessment', `/assessments/${id}`);
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
    if (!this.state.validate(this.answers)) return;
    void this.state.run(async () => {
      await this.api.request('assessment', `/assessments/${this.selected()?.id}/submissions`, 'POST', {
        answers: this.answers.getRawValue(),
      });
      this.answers.markAsPristine();
      this.submitted.set(true);
    }, 'Answers submitted. Your teacher will review the grade.');
  }
}
