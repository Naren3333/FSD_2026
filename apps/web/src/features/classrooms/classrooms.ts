import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { IdentityApi } from '../../api/clients';
import type { Classroom, Student } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { studentName } from '../../shared/names';
import { PagerComponent } from '../../shared/pager';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

@Component({
  selector: 'app-classrooms',
  standalone: true,
  imports: [
    ReactiveFormsModule,
    FieldComponent,
    StatusComponent,
    SectionStateComponent,
    PagerComponent,
    ConfirmDialogComponent,
  ],
  templateUrl: './classrooms.html',
})
export class ClassroomsComponent {
  readonly session = inject(Session);
  private readonly context = inject(ClassroomContext);
  private readonly identity = inject(IdentityApi);
  readonly classrooms = this.context.classrooms;
  readonly classroomId = this.context.classroomId;
  readonly classroomName = this.context.classroomName;
  readonly state = new ActionState();
  readonly rosterState = new ActionState();
  readonly roster = new Section<Student[]>();
  /** The page of school students on show, fetched from the server ten at a time, and how many match the search
   * across every page. */
  readonly everyone = new Section<{ students: Student[]; total: number }>();
  readonly enrolled = computed(() => new Set((this.roster.value() ?? []).map((student) => student.id)));
  readonly everyoneState = new ActionState();
  /** What the teacher typed to narrow the list, matched by the server against names and usernames. */
  readonly search = signal('');
  readonly page = signal(0);
  readonly pageSize = 10;
  private searchTimer?: ReturnType<typeof setTimeout>;
  /** The student the remove dialog is asking about. */
  readonly removing = signal<Student | null>(null);
  readonly studentName = studentName;
  @ViewChild('removeDialog') removeDialog?: ConfirmDialogComponent;
  /** The classroom the delete dialog is asking about. */
  readonly deleting = signal<Classroom | null>(null);
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  readonly form = new FormGroup({
    name: new FormControl('', { nonNullable: true, validators: requiredText(120) }),
  });

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
    onClassroomChange(() => this.loadRoster());
    this.loadEveryone();
  }

  /** Narrows the list and goes back to its first page once the teacher pauses typing. */
  searchFor(words: string): void {
    this.search.set(words);
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => this.goTo(0), 250);
  }

  goTo(page: number): void {
    this.page.set(Math.max(0, page));
    this.loadEveryone();
  }

  /** Fetches the page on show; a page past the end, after students leave, falls back to the last one. */
  loadEveryone(): void {
    if (!this.session.isTeacher()) return;
    void this.everyone.load(async (current) => {
      const search = this.search().trim();
      let result = await this.identity.students(search, this.page() * this.pageSize, this.pageSize);
      if (!result.students.length && result.total && this.page() > 0) {
        const last = Math.ceil(result.total / this.pageSize) - 1;
        result = await this.identity.students(search, last * this.pageSize, this.pageSize);
        if (current()) this.page.set(last);
      }
      return result;
    });
  }

  /** Enrolls a student from the school list into the current classroom. */
  enrollStudent(student: Student): void {
    const classroomId = this.classroomId();
    if (!classroomId) return;
    void this.everyoneState.run(
      async () => {
        await this.identity.enroll(classroomId, student.id);
        this.roster.set(await this.identity.roster(classroomId));
      },
      `${this.studentName(student)} enrolled.`,
    );
  }

  refresh(): void {
    void this.state.run(() => this.context.refresh());
  }

  /** The current classroom's students, for its teacher. */
  loadRoster(): void {
    const classroomId = this.classroomId();
    this.roster.clear();
    if (!classroomId || !this.session.isTeacher()) return;
    void this.roster.load(() => this.identity.roster(classroomId));
  }

  create(): void {
    if (!this.state.validate(this.form)) return;
    void this.state.run(async () => {
      await this.identity.createClassroom(this.form.controls.name.value);
      this.form.reset();
      await this.context.refresh();
    }, 'Classroom created.');
  }

  confirmRemove(student: Student): void {
    this.removing.set(student);
    this.removeDialog?.show();
  }

  closeRemove(): void {
    this.removing.set(null);
    this.removeDialog?.close();
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
    this.deleteDialog?.show();
  }

  closeDelete(): void {
    this.deleting.set(null);
    this.deleteDialog?.close();
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
