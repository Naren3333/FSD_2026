import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { IdentityApi } from '../../api/clients';
import type { Classroom, Student, StudentPage } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { NoClassroomComponent } from '../../shared/no-classroom';
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
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    PagerComponent,
    ConfirmDialogComponent,
    NoClassroomComponent,
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
  /** The page of the classroom's students on show, by name, and how many are enrolled. */
  readonly roster = new Section<StudentPage>();
  readonly rosterPage = signal(0);
  readonly rosterPageSize = 20;
  /** Every enrolled student's id, however many pages the roster has, to mark them in the school list. */
  readonly enrolledIds = new Section<string[]>();
  /** The page of school students on show, fetched from the server ten at a time, and how many match the search
   * across every page. */
  readonly everyone = new Section<StudentPage>();
  readonly enrolled = computed(() => new Set(this.enrolledIds.value() ?? []));
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
    onClassroomChange(() => {
      this.rosterPage.set(0);
      this.loadRoster();
    });
    this.loadEveryone();
  }

  /** Narrows the list and goes back to its first page once the teacher pauses typing. */
  searchFor(words: string): void {
    this.search.set(words);
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => {
      this.page.set(0);
      this.loadEveryone();
    }, 250);
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
        await this.rereadRoster(classroomId);
      },
      `${this.studentName(student)} enrolled.`,
    );
  }

  refresh(): void {
    void this.state.run(() => this.context.refresh());
  }

  /** The current classroom's students, for its teacher: the page on show and every enrolled id. */
  loadRoster(): void {
    const classroomId = this.classroomId();
    this.roster.clear();
    this.enrolledIds.clear();
    if (!classroomId || !this.session.isTeacher()) return;
    void this.roster.load((current) => this.readRoster(classroomId, current));
    void this.enrolledIds.load(() => this.identity.rosterIds(classroomId));
  }

  /** Shows the roster page the pager moved to. */
  showRosterPage(): void {
    const classroomId = this.classroomId();
    if (classroomId) void this.roster.load((current) => this.readRoster(classroomId, current));
  }

  /** The roster page on show; a page past the end, after students leave, falls back to the last one. */
  private async readRoster(classroomId: string, current: () => boolean): Promise<StudentPage> {
    const size = this.rosterPageSize;
    let result = await this.identity.rosterPage(classroomId, this.rosterPage() * size, size);
    if (!result.students.length && result.total && this.rosterPage() > 0) {
      const last = Math.ceil(result.total / size) - 1;
      result = await this.identity.rosterPage(classroomId, last * size, size);
      if (current()) this.rosterPage.set(last);
    }
    return result;
  }

  /** Re-reads the roster page and the enrolled ids after a student joined or left. */
  private async rereadRoster(classroomId: string): Promise<void> {
    const same = () => this.classroomId() === classroomId;
    await Promise.all([
      this.roster.reload(() => this.readRoster(classroomId, same)),
      this.enrolledIds.reload(() => this.identity.rosterIds(classroomId)),
    ]);
  }

  create(): void {
    if (!this.state.validate(this.form, 'classroom-form')) return;
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
      await this.rereadRoster(classroomId);
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
