import { Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, Classroom } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';

@Component({
  selector: 'app-classrooms',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './classrooms.html',
})
export class ClassroomsComponent {
  readonly api = inject(Api);
  readonly state = new ActionState();
  @Input() classrooms: Classroom[] = [];
  @Input() classroomId = '';
  @Output() refresh = new EventEmitter<void>();
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(120), Validators.pattern(/\S/)],
    }),
  });
  readonly enrollment = new FormGroup({
    student_id: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });
  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.api.request('identity', '/classrooms', 'POST', this.form.getRawValue());
      this.form.reset();
      this.refresh.emit();
    }, 'Classroom created.');
  }
  enroll(): void {
    if (!this.state.validate(this.enrollment)) return;
    void this.state.run(async () => {
      await this.api.request(
        'identity',
        `/classrooms/${this.classroomId}/enrollments`,
        'POST',
        this.enrollment.getRawValue(),
      );
      this.enrollment.reset();
    }, 'Student enrolled.');
  }
}
