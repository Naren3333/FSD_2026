import { Component, Input, OnChanges, inject, signal } from '@angular/core';
import { DecimalPipe, PercentPipe } from '@angular/common';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { Api, Performance } from './api';
import { ActionState, FieldComponent, StatusComponent } from './shared';
@Component({
  selector: 'app-performance',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent, PercentPipe, DecimalPipe],
  templateUrl: './performance.html',
})
export class PerformanceComponent implements OnChanges {
  readonly api = inject(Api);
  readonly state = new ActionState();
  @Input() classroomId = '';
  readonly result = signal<Performance | null>(null);
  readonly form = new FormGroup({
    student: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });
  ngOnChanges(): void {
    this.result.set(null);
    if (this.api.profile()?.role === 'student') this.form.controls.student.setValue(this.api.profile()!.id);
  }
  load(): void {
    if (!this.state.validate(this.form)) return;
    this.result.set(null);
    void this.state.run(async () => {
      this.result.set(
        await this.api.request(
          'analytics',
          `/performance/${encodeURIComponent(this.form.controls.student.value)}?classroom_id=${this.classroomId}`,
        ),
      );
      this.form.markAsPristine();
    });
  }
}
