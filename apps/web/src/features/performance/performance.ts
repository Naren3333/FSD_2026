import { DecimalPipe, PercentPipe } from '@angular/common';
import { Component, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { AnalyticsApi, CurriculumApi, IdentityApi } from '../../api/clients';
import type { Performance, Student } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { skillNames, studentName } from '../../shared/names';
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
  private readonly identity = inject(IdentityApi);
  private readonly curriculum = inject(CurriculumApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly result = signal<Performance | null>(null);
  /** The classroom's students a teacher can choose from. */
  readonly roster = signal<Student[]>([]);
  private readonly skills = signal(new Map<string, string>());
  readonly studentName = studentName;
  readonly form = new FormGroup({
    student: new FormControl('', { nonNullable: true, validators: [Validators.required] }),
  });

  constructor() {
    onClassroomChange((classroomId) => {
      this.result.set(null);
      this.roster.set([]);
      // Students only ever see their own performance; teachers choose from the classroom's roster.
      const profile = this.session.profile();
      if (profile?.role === 'student') {
        this.form.controls.student.setValue(profile.id);
      } else {
        this.form.reset();
        if (classroomId) {
          void this.state.run(async () => this.roster.set(await this.identity.roster(classroomId)));
        }
      }
    });
  }

  skill(skillId: string): string {
    return this.skills().get(skillId) ?? '';
  }

  load(): void {
    if (!this.state.validate(this.form)) return;
    this.result.set(null);
    void this.state.run(async () => {
      const [performance, nodes] = await Promise.all([
        this.analytics.performance(this.form.controls.student.value, this.classroomId()),
        this.curriculum.nodes(),
      ]);
      this.skills.set(skillNames(nodes));
      this.result.set(performance);
      this.form.markAsPristine();
    });
  }
}
