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
  /** The page of school students on show, fetched from the server ten at a time. */
  readonly shown = signal<Student[]>([]);
  /** How many students in the school match the search, across every page. */
  readonly total = signal(0);
  readonly enrolled = computed(() => new Set(this.roster().map((student) => student.id)));
  readonly everyoneState = new ActionState();
  /** What the teacher typed to narrow the list, matched by the server against names and usernames. */
  readonly search = signal('');
  readonly page = signal(0);
  readonly pageSize = 10;
  readonly pages = computed(() => Math.max(1, Math.ceil(this.total() / this.pageSize)));
  private searchTimer?: ReturnType<typeof setTimeout>;
  /** Counts page requests so a slow, older response never replaces a newer one. */
  private request = 0;
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

  constructor() {
    inject(UnsavedChanges).track(() => this.form.dirty);
    onClassroomChange(() => {
      this.roster.set([]);
      this.loadRoster();
    });
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
    const request = ++this.request;
    void this.everyoneState.run(async () => {
      const search = this.search().trim();
      let result = await this.identity.students(search, this.page() * this.pageSize, this.pageSize);
      if (!result.students.length && result.total && this.page() > 0) {
        const last = Math.ceil(result.total / this.pageSize) - 1;
        result = await this.identity.students(search, last * this.pageSize, this.pageSize);
        if (request === this.request) this.page.set(last);
      }
      if (request !== this.request) return;
      this.shown.set(result.students);
      this.total.set(result.total);
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
