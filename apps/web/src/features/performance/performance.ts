import { DecimalPipe, PercentPipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { AnalyticsApi } from '../../api/clients';
import type { Performance } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { StatusComponent } from '../../shared/status';

@Component({
  selector: 'app-performance',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent, PercentPipe, DecimalPipe],
  templateUrl: './performance.html',
})
export class PerformanceComponent {
  readonly session = inject(Session);
  private readonly analytics = inject(AnalyticsApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly result = signal<Performance | null>(null);
  readonly form = new FormGroup({
    student: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });

  constructor() {
    onClassroomChange(() => {
      this.result.set(null);
      // Students only ever see their own performance.
      const profile = this.session.profile();
      if (profile?.role === 'student') this.form.controls.student.setValue(profile.id);
    });
  }

  load(): void {
    if (!this.state.validate(this.form)) return;
    this.result.set(null);
    void this.state.run(async () => {
      this.result.set(await this.analytics.performance(this.form.controls.student.value, this.classroomId()));
      this.form.markAsPristine();
    });
  }
}
