import { Component, ElementRef, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { IdentityApi } from '../../api/clients';
import type { Classroom, Student } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { FieldComponent } from '../../shared/field';
import { studentName } from '../../shared/names';
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
  readonly classroomName = computed(
    () => this.classrooms().find((room) => room.id === this.classroomId())?.name ?? '',
  );
  readonly state = new ActionState();
  readonly rosterState = new ActionState();
  readonly roster = signal<Student[]>([]);
  /** The student the remove dialog is asking about. */
  readonly removing = signal<Student | null>(null);
  readonly studentName = studentName;
  @ViewChild('removeDialog') removeDialog?: ElementRef<HTMLDialogElement>;
  /** The classroom the delete dialog is asking about. */
  readonly deleting = signal<Classroom | null>(null);
  @ViewChild('deleteDialog') deleteDialog?: ElementRef<HTMLDialogElement>;
  readonly form = new FormGroup({
    name: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(120), Validators.pattern(/\S/)],
    }),
  });
  readonly enrollment = new FormGroup({
    username: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(/\S/)],
    }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty || this.enrollment.dirty);
    onClassroomChange(() => {
      this.roster.set([]);
      this.loadRoster();
    });
  }

  refresh(): void {
    void this.state.run(() => this.context.refresh());
  }

  /** The current classroom's students, for its teacher. */
  loadRoster(): void {
    const classroomId = this.classroomId();
    if (!classroomId || !this.session.isTeacher()) return;
    void this.rosterState.run(async () => this.roster.set(await this.identity.roster(classroomId)));
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
    const classroomId = this.classroomId();
    void this.state.run(async () => {
      await this.identity.enroll(classroomId, this.enrollment.controls.username.value.trim());
      this.enrollment.reset();
      this.roster.set(await this.identity.roster(classroomId));
    }, 'Student enrolled.');
  }

  confirmRemove(student: Student): void {
    this.removing.set(student);
    this.removeDialog?.nativeElement.showModal();
  }

  closeRemove(): void {
    this.removing.set(null);
    this.removeDialog?.nativeElement.close();
  }

  remove(): void {
    const student = this.removing();
    const classroomId = this.classroomId();
    if (!student) return;
    void this.rosterState.run(async () => {
      await this.identity.unenroll(classroomId, student.id);
      this.closeRemove();
      this.roster.set(await this.identity.roster(classroomId));
    }, 'Student removed from the classroom.');
  }

  confirmDelete(classroom: Classroom): void {
    this.deleting.set(classroom);
    this.deleteDialog?.nativeElement.showModal();
  }

  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.nativeElement.close();
  }

  deleteClassroom(): void {
    const classroom = this.deleting();
    if (!classroom) return;
    void this.state.run(async () => {
      await this.identity.deleteClassroom(classroom.id);
      this.closeDelete();
      await this.context.refresh();
    }, 'Classroom deleted.');
  }
}
