import { Component, ElementRef, Input, OnChanges, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, Evaluation } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';
@Component({
  selector: 'app-review',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './review.html',
})
export class ReviewComponent implements OnChanges {
  readonly api = inject(Api);
  readonly state = new ActionState();
  @Input() classroomId = '';
  readonly grades = signal<Evaluation[]>([]);
  readonly selected = signal<Evaluation | null>(null);
  @ViewChild('dialog') dialog?: ElementRef<HTMLDialogElement>;
  readonly form = new FormGroup({
    reason: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(1000), Validators.pattern(/\S/)],
    }),
  });
  ngOnChanges(): void {
    this.grades.set([]);
    this.load();
  }
  load(): void {
    if (!this.classroomId) return;
    void this.state.run(async () => this.grades.set(await this.api.evaluations(this.classroomId)));
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
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.api.request('grading', `/evaluations/${this.selected()?.id}/finalize`, 'POST', {
        reason: this.form.controls.reason.value,
        overrides: {},
      });
      this.close();
      this.grades.set(await this.api.evaluations(this.classroomId));
    }, 'Grade finalized. Performance will update after event processing.');
  }
}
