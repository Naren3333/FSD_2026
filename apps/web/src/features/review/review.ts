import { Component, ElementRef, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { AssessmentApi, CurriculumApi, GradingApi, IdentityApi } from '../../api/clients';
import type { Assessment, Evaluation, Evidence } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { assessmentsOf, promptOf, skillNames, studentNames, total } from '../../shared/names';
import { StatusComponent } from '../../shared/status';

@Component({
  selector: 'app-review',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, StatusComponent],
  templateUrl: './review.html',
})
export class ReviewComponent {
  private readonly grading = inject(GradingApi);
  private readonly identity = inject(IdentityApi);
  private readonly assessmentApi = inject(AssessmentApi);
  private readonly curriculum = inject(CurriculumApi);
  readonly classroomId = inject(ClassroomContext).classroomId;
  readonly state = new ActionState();
  readonly grades = signal<Evaluation[]>([]);
  private readonly students = signal(new Map<string, string>());
  private readonly skills = signal(new Map<string, string>());
  private readonly assessments = signal(new Map<string, Assessment>());
  readonly total = total;
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
    void this.state.run(async () => this.show(await this.grading.evaluations(classroomId), classroomId));
  }

  /** Shows the evaluations with the names of their students, assessments, questions and skills. */
  private async show(grades: Evaluation[], classroomId: string): Promise<void> {
    const [roster, nodes, assessments] = await Promise.all([
      this.identity.roster(classroomId),
      this.curriculum.nodes(),
      assessmentsOf(grades, (id) => this.assessmentApi.get(id)),
    ]);
    this.students.set(studentNames(roster));
    this.skills.set(skillNames(nodes));
    this.assessments.set(assessments);
    this.grades.set(grades);
  }

  /** The student's roster name, or '' for a student no longer enrolled or not yet named. */
  studentName(grade: Evaluation): string {
    return this.students().get(grade.student_id) ?? '';
  }
  title(grade: Evaluation): string {
    return this.assessments().get(grade.assessment_id)?.title ?? '';
  }
  prompt(grade: Evaluation, evidence: Evidence): string {
    return promptOf(this.assessments(), grade.assessment_id, evidence.question_id);
  }
  skill(evidence: Evidence): string {
    return this.skills().get(evidence.skill_id) ?? '';
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
      const classroomId = this.classroomId();
      await this.show(await this.grading.evaluations(classroomId), classroomId);
    }, 'Grade finalized. Performance will update after event processing.');
  }
}
