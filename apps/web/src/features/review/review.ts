import { Component, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { AssessmentApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { CurriculumNames } from '../../core/curriculum-names';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { MarksPipe } from '../../shared/marks';
import { assessmentsOf, promptOf, studentNames } from '../../shared/names';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

/** A classroom's evaluations with the names of their students and assessments. */
interface Grades {
  grades: Evaluation[];
  students: Map<string, string>;
  assessments: Map<string, Assessment>;
}

@Component({
  selector: 'app-review',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    StatusComponent,
    SectionStateComponent,
    ConfirmDialogComponent,
    MarksPipe,
  ],
  templateUrl: './review.html',
})
export class ReviewComponent {
  private readonly grading = inject(GradingApi);
  private readonly identity = inject(IdentityApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumNames);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly grades = new Section<Grades>();
  readonly selected = signal<Evaluation | null>(null);
  @ViewChild('dialog') dialog?: ConfirmDialogComponent;
  readonly form = new FormGroup({
    reason: new FormControl('', { nonNullable: true, validators: requiredText(1000) }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
    onClassroomChange(() => this.load());
  }

  load(): void {
    const classroomId = this.classroomId();
    this.grades.clear();
    if (!classroomId) return;
    void this.grades.load(() => this.read(classroomId));
  }

  /** The evaluations with the names of their students, assessments, questions and skills. */
  private async read(classroomId: string): Promise<Grades> {
    const grades = await this.grading.evaluations(classroomId);
    const [roster, assessments] = await Promise.all([
      this.identity.roster(classroomId),
      assessmentsOf(grades, (id) => this.assessmentApi.get(id)),
      this.curriculum.need(grades.flatMap((g) => g.evidence.map((e) => e.skill_id))),
    ]);
    return { grades, students: studentNames(roster), assessments };
  }

  /** The student's roster name, or '' for a student no longer enrolled or not yet named. */
  studentName(grade: Evaluation): string {
    return this.grades.value()?.students.get(grade.student_id) ?? '';
  }
  title(grade: Evaluation): string {
    return this.grades.value()?.assessments.get(grade.assessment_id)?.title ?? '';
  }
  prompt(grade: Evaluation, evidence: Evidence): string {
    return promptOf(this.grades.value()?.assessments ?? new Map(), grade.assessment_id, evidence.question_id);
  }
  skill(evidence: Evidence): string {
    return this.curriculum.skillName(evidence.skill_id);
  }

  review(grade: Evaluation): void {
    this.selected.set(grade);
    this.form.reset();
    this.dialog?.show();
  }

  close(): void {
    this.form.reset();
    this.selected.set(null);
    this.dialog?.close();
  }

  finalize(): void {
    const grade = this.selected();
    if (!grade || !this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.grading.finalize(grade.id, this.form.controls.reason.value);
      this.close();
      this.grades.set(await this.read(this.classroomId()));
    }, 'Grade finalized. Performance will update after event processing.');
  }
}
