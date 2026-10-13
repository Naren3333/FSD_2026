import { Component, ElementRef, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { GradingApi } from '../../api/clients';
import type { Evaluation } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { StatusComponent } from '../../shared/status';

@Component({
  selector: 'app-review',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './review.html',
})
export class ReviewComponent {
  private readonly grading = inject(GradingApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly grades = signal<Evaluation[]>([]);
  readonly selected = signal<Evaluation | null>(null);
  @ViewChild('dialog') dialog?: ElementRef<HTMLDialogElement>;
  readonly form = new FormGroup({
    reason: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(1000), Validators.pattern(/\S/)],
    }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
    onClassroomChange(() => {
      this.grades.set([]);
      this.load();
    });
  }

  load(): void {
    const classroomId = this.classroomId();
    if (!classroomId) return;
    void this.state.run(async () => this.grades.set(await this.grading.evaluations(classroomId)));
  }

  review(grade: Evaluation): void {
    this.selected.set(grade);
    this.form.reset();
    this.dialog?.nativeElement.showModal();
  }

  close(): void {
    this.form.reset();
    this.selected.set(null);
    this.dialog?.nativeElement.close();
  }

  finalize(): void {
    const grade = this.selected();
    if (!grade || !this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.grading.finalize(grade.id, this.form.controls.reason.value);
      this.close();
      this.grades.set(await this.grading.evaluations(this.classroomId()));
    }, 'Grade finalized. Performance will update after event processing.');
  }
}
