import { DecimalPipe, PercentPipe } from '@angular/common';
import { Component, computed, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { AnalyticsApi, IdentityApi } from '../../api/clients';
import type { Performance, Student } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { studentName } from '../../shared/names';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';

@Component({
  selector: 'app-performance',
  standalone: true,
  imports: [
    NoClassroomComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    PercentPipe,
    DecimalPipe,
  ],
  templateUrl: './performance.html',
})
export class PerformanceComponent {
  readonly session = inject(Session);
  private readonly analytics = inject(AnalyticsApi);
  private readonly identity = inject(IdentityApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  /* only the form's validation; the evidence and roster report their own loading and errors. */
  readonly state = new ActionState();
  readonly result = new Section<Performance>();
  /* the classroom's students a teacher can choose from. */
  readonly roster = new Section<Student[]>();
  readonly rosterHint = computed(() =>
    this.roster.loading()
      ? 'Loading students…'
      : this.roster.value()?.length === 0
        ? 'No students are enrolled in this classroom yet.'
        : '',
  );
  readonly studentName = studentName;
  /* the student a link such as the class dashboard asked to open, until they are found on the roster. */
  private requested = '';
  readonly form = new FormGroup({
    student: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });

  constructor() {
    onClassroomChange((classroomId) => {
      this.result.clear();
      this.roster.clear();
      // students only ever see their own performance; teachers choose from the classroom's roster.
      const profile = this.session.profile();
      if (profile && this.session.isStudent()) {
        this.form.controls.student.setValue(profile.id);
      } else {
        this.form.reset();
        if (classroomId) {
          void this.roster.load(() => this.identity.roster(classroomId)).then(() => this.openRequested());
        }
      }
    });
    inject(ActivatedRoute)
      .queryParamMap.pipe(takeUntilDestroyed())
      .subscribe((params) => {
        this.requested = params.get('student') ?? '';
        this.openRequested();
      });
  }

  /* shows the requested student's evidence once they are on the current classroom's roster. */
  private openRequested(): void {
    if (!this.requested || !this.roster.value()?.some((s) => s.id === this.requested)) return;
    this.form.controls.student.setValue(this.requested);
    this.requested = '';
    this.load();
  }

  topic(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  load(): void {
    if (!this.state.validate(this.form, 'performance-form')) return;
    this.state.error.set('');
    const studentId = this.form.controls.student.value;
    const classroomId = this.classroomId();
    this.result.clear();
    void this.result.load(async () => {
      const performance = await this.analytics.performance(studentId, classroomId);
      // without the curriculum, topics show their ids.
      await this.curriculum.need(performance.topics.map((s) => s.topic_id)).catch(() => undefined);
      this.form.markAsPristine();
      return performance;
    });
  }
}
