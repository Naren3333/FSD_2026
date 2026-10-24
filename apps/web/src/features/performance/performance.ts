import { DecimalPipe, PercentPipe } from '@angular/common';
import { Component, computed, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { FormControl, ReactiveFormsModule } from '@angular/forms';
import { ActivatedRoute } from '@angular/router';
import { AnalyticsApi, IdentityApi } from '../../api/clients';
import type { Performance, Student } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { Session } from '../../core/session';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { studentName } from '../../shared/names';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { InterpretationComponent } from './interpretation';

@Component({
  selector: 'app-performance',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    NoClassroomComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    SectionStateComponent,
    PercentPipe,
    DecimalPipe,
    InterpretationComponent,
  ],
  templateUrl: './performance.html',
})
export class PerformanceComponent {
  readonly session = inject(Session);
  private readonly analytics = inject(AnalyticsApi);
  private readonly identity = inject(IdentityApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
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
  /* the student whose evidence is on show: the signed-in student, or the one a teacher chose. */
  readonly student = new FormControl('', { nonNullable: true });

  constructor() {
    onClassroomChange((classroomId) => {
      this.result.clear();
      this.roster.clear();
      // students only ever see their own performance; teachers choose from the classroom's roster.
      const profile = this.session.profile();
      if (profile && this.session.isStudent()) {
        this.student.setValue(profile.id);
        this.load();
      } else {
        this.student.reset();
        if (classroomId) {
          void this.roster.load(() => this.identity.roster(classroomId)).then(() => this.openRequested());
        }
      }
    });
    onSectionShown('performance', () => {
      const studentId = this.student.value;
      const classroomId = this.classroomId();
      if (studentId && classroomId) rereadQuietly(this.result, () => this.read(studentId, classroomId));
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
    this.student.setValue(this.requested);
    this.requested = '';
    this.load();
  }

  topic(topicId: string): string {
    return this.curriculum.topicName(topicId);
  }

  /* a teacher chose a student, or the empty option, which clears the evidence. */
  choose(studentId: string): void {
    this.student.setValue(studentId);
    this.load();
  }

  load(): void {
    const studentId = this.student.value;
    const classroomId = this.classroomId();
    this.result.clear();
    if (!studentId || !classroomId) return;
    void this.result.load(() => this.read(studentId, classroomId));
  }

  private async read(studentId: string, classroomId: string): Promise<Performance> {
    const performance = await this.analytics.performance(studentId, classroomId);
    // without the curriculum, topics show their ids.
    await this.curriculum.need(performance.topics.map((s) => s.topic_id)).catch(() => undefined);
    return performance;
  }
}
