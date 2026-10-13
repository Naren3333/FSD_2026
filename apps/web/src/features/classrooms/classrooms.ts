import { Component, inject } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { IdentityApi } from '../../api/clients';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { StatusComponent } from '../../shared/status';

@Component({
  selector: 'app-classrooms',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './classrooms.html',
})
export class ClassroomsComponent {
  readonly session = inject(Session);
  private readonly context = inject(ClassroomContext);
  private readonly identity = inject(IdentityApi);
  readonly classrooms = this.context.classrooms;
  readonly classroomId = this.context.classroomId;
  readonly state = new ActionState();
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(120), Validators.pattern(/\S/)],
    }),
  });
  readonly enrollment = new FormGroup({
    student_id: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.enrollment.dirty);
  }

  refresh(): void {
    void this.state.run(() => this.context.refresh());
  }

  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.identity.createClassroom(this.form.controls.name.value);
      this.form.reset();
      await this.context.refresh();
    }, 'Classroom created.');
  }

  enroll(): void {
    if (!this.state.validate(this.enrollment)) return;
    void this.state.run(async () => {
      await this.identity.enroll(this.classroomId(), this.enrollment.controls.student_id.value);
      this.enrollment.reset();
    }, 'Student enrolled.');
  }
}
