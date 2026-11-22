import { Component, DestroyRef, ElementRef, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { AdminApi } from '../../api/clients';
import type {
  Account,
  AccountPage,
  SchoolClassroom,
  SchoolClassroomPage,
  Student,
  StudentPage,
} from '../../api/models';
import { ADMIN_ACCOUNTS_PATH } from '../../app.routes';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { studentName } from '../../shared/names';
import { PageHeaderComponent } from '../../shared/page-header';
import { readPage } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import { plural } from '../../shared/format';

/* the school admin's classrooms (adr 0019): create one for a teacher, rename it or give it another teacher, delete it, and enroll or remove its students. */
@Component({
  selector: 'app-admin-classrooms',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    PagerComponent,
    ConfirmDialogComponent,
    IconComponent,
    RouterLink,
  ],
  templateUrl: './classrooms.html',
})
export class AdminClassroomsComponent {
  private readonly admin = inject(AdminApi);
  private readonly unsaved = inject(UnsavedChanges);
  readonly accountsPath = ADMIN_ACCOUNTS_PATH;
  readonly studentName = studentName;

  /* the school's teachers, for the teacher select. */
  readonly teachers = new Section<Account[]>();
  /* the page of the school's classrooms on show, newest first, and how many there are. */
  readonly classrooms = new Section<SchoolClassroomPage>();
  readonly page = signal(0);
  readonly pageSize = 20;
  readonly state = new ActionState();

  /* the classroom being renamed or given another teacher; null while the form creates one. */
  readonly editing = signal<SchoolClassroom | null>(null);
  readonly form = new FormGroup({
    name: new FormControl('', { nonNullable: true, validators: requiredText(120) }),
    teacher: new FormControl('', { nonNullable: true, validators: Validators.required }),
  });
  @ViewChild('formHeading') private formHeading?: ElementRef<HTMLElement>;

  /* the classroom whose students are open below the list. */
  readonly managing = signal<SchoolClassroom | null>(null);
  readonly roster = new Section<StudentPage>();
  readonly rosterPage = signal(0);
  readonly rosterPageSize = 20;
  /* every enrolled student's id, to mark them in the school's list. */
  readonly enrolledIds = new Section<Set<string>>();
  readonly rosterState = new ActionState();
  readonly everyone = new Section<AccountPage>();
  readonly everyoneState = new ActionState();
  readonly search = signal('');
  readonly everyonePage = signal(0);
  readonly everyonePageSize = 10;
  private searchTimer?: ReturnType<typeof setTimeout>;
  @ViewChild('studentsHeading') private studentsHeading?: ElementRef<HTMLElement>;

  /* the classroom the delete dialog asks about, and the student the remove dialog asks about. */
  readonly deleting = signal<SchoolClassroom | null>(null);
  readonly removing = signal<Student | null>(null);
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  @ViewChild('removeDialog') removeDialog?: ConfirmDialogComponent;
  /* the classroom to edit once the admin agrees to discard the form's unsaved changes. */
  readonly pendingEdit = signal<SchoolClassroom | null>(null);
  readonly discardState = new ActionState();
  @ViewChild('discardDialog') discardDialog?: ConfirmDialogComponent;

  readonly subtitle = computed(() => {
    const total = this.classrooms.ready() ? this.classrooms.value()?.total : undefined;
    return total === undefined
      ? 'Create classrooms, give each one a teacher and enroll its students.'
      : `${plural(total, 'classroom')} in your school`;
  });
  readonly noTeachers = computed(() => this.teachers.ready() && !this.teachers.value()?.length);

  constructor() {
    this.unsaved.track(() => this.form.dirty);
    // a search typed just before leaving is not sent after.
    inject(DestroyRef).onDestroy(() => clearTimeout(this.searchTimer));
    this.refresh();
  }

  /* re-reads the teachers and the classrooms page on show, and the open classroom's students. */
  refresh(): void {
    void this.teachers.load(() => this.admin.teachers());
    this.loadClassrooms();
    if (this.managing()) {
      this.loadRoster();
      this.loadEveryone();
    }
  }

  loadClassrooms(): void {
    void this.classrooms.load((current) => this.readClassrooms(current));
  }

  /* the classrooms page on show; a page past the end, after a deletion, falls back to the last one. */
  private async readClassrooms(current: () => boolean): Promise<SchoolClassroomPage> {
    const { result, page } = await readPage(
      this.page(),
      this.pageSize,
      (offset, limit) => this.admin.classrooms(offset, limit),
      (r) => !r.items.length,
    );
    if (current()) this.page.set(page);
    return result;
  }

  private rereadClassrooms(): Promise<void> {
    return this.classrooms.reload(() => this.readClassrooms(() => true));
  }

  /* creates the classroom, or saves the one being edited. */
  save(): void {
    if (!this.state.validate(this.form, 'classroom-form')) return;
    const { name, teacher } = this.form.getRawValue();
    const editing = this.editing();
    void this.state.run(
      async () => {
        if (editing) await this.admin.updateClassroom(editing.id, name.trim(), teacher);
        else await this.admin.createClassroom(name.trim(), teacher);
        this.clearForm();
        await this.rereadClassrooms();
        if (editing && this.managing()?.id === editing.id) this.managing.set(this.listed(editing.id) ?? null);
      },
      editing ? 'Classroom saved.' : 'Classroom created.',
    );
  }

  /* fills the form with the classroom, to rename it or give it another teacher, first asking before it
     replaces changes not yet saved. */
  edit(classroom: SchoolClassroom): void {
    if (!this.form.dirty) {
      this.startEdit(classroom);
      return;
    }
    this.pendingEdit.set(classroom);
    this.discardDialog?.show();
  }

  /* discards the unsaved changes and edits the classroom the admin chose. */
  discardAndEdit(): void {
    const classroom = this.pendingEdit();
    this.discardDialog?.close();
    if (classroom) this.startEdit(classroom);
  }

  closeDiscard(): void {
    this.pendingEdit.set(null);
    this.discardDialog?.close();
  }

  private startEdit(classroom: SchoolClassroom): void {
    this.editing.set(classroom);
    this.form.reset({ name: classroom.name, teacher: classroom.teacher_id });
    this.state.message.set('');
    this.state.error.set('');
    setTimeout(() => this.formHeading?.nativeElement.focus());
  }

  clearForm(): void {
    this.editing.set(null);
    this.form.reset({ name: '', teacher: '' });
  }

  /* the classroom's name for its teacher id in the select, or the stored name when that teacher is not listed. */
  teacherName(classroom: SchoolClassroom): string {
    return classroom.teacher_name || classroom.teacher_id;
  }

  private listed(id: string): SchoolClassroom | undefined {
    return this.classrooms.value()?.items.find((c) => c.id === id);
  }

  confirmDelete(classroom: SchoolClassroom): void {
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
      await this.admin.deleteClassroom(classroom.id);
      this.closeDelete();
      if (this.editing()?.id === classroom.id) this.clearForm();
      if (this.managing()?.id === classroom.id) this.managing.set(null);
      await this.rereadClassrooms();
    }, `${classroom.name} deleted.`);
  }

  /* opens the classroom's students below the list. */
  manage(classroom: SchoolClassroom): void {
    this.managing.set(classroom);
    this.rosterPage.set(0);
    this.loadRoster();
    this.loadEveryone();
    setTimeout(() => this.studentsHeading?.nativeElement.focus());
  }

  closeManage(): void {
    this.managing.set(null);
    this.roster.clear();
    this.enrolledIds.clear();
  }

  loadRoster(): void {
    const classroom = this.managing();
    if (!classroom) return;
    void this.roster.load((current) => this.readRoster(classroom.id, current));
    void this.enrolledIds.load(() => this.readEnrolled(classroom.id));
  }

  showRosterPage(): void {
    const classroom = this.managing();
    if (classroom) void this.roster.load((current) => this.readRoster(classroom.id, current));
  }

  private async readRoster(classroomId: string, current: () => boolean): Promise<StudentPage> {
    const { result, page } = await readPage(
      this.rosterPage(),
      this.rosterPageSize,
      (offset, limit) => this.admin.rosterPage(classroomId, offset, limit),
      (r) => !r.students.length,
    );
    if (current()) this.rosterPage.set(page);
    return result;
  }

  /* the ids of everyone enrolled, in one read. */
  private async readEnrolled(classroomId: string): Promise<Set<string>> {
    return new Set(await this.admin.rosterIds(classroomId));
  }

  /* re-reads the roster, the enrolled ids and the classrooms' counts after a student joined or left. */
  private async rereadRoster(classroomId: string): Promise<void> {
    const same = () => this.managing()?.id === classroomId;
    // another classroom opened meanwhile: its own reads are current, so only the counts are re-read.
    if (!same()) return this.rereadClassrooms();
    await Promise.all([
      this.roster.reload(() => this.readRoster(classroomId, same)),
      this.enrolledIds.reload(() => this.readEnrolled(classroomId)),
      this.rereadClassrooms(),
    ]);
  }

  /* narrows the school's students and goes back to the first page once the admin pauses typing. */
  searchFor(words: string): void {
    this.search.set(words);
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => {
      this.everyonePage.set(0);
      this.loadEveryone();
    }, 250);
  }

  loadEveryone(): void {
    void this.everyone.load((current) => this.readEveryone(current));
  }

  private async readEveryone(current: () => boolean): Promise<AccountPage> {
    const search = this.search().trim();
    const { result, page } = await readPage(
      this.everyonePage(),
      this.everyonePageSize,
      (offset, limit) => this.admin.accounts('student', search, offset, limit),
      (r) => !r.accounts.length,
    );
    if (current()) this.everyonePage.set(page);
    return result;
  }

  enroll(student: Account): void {
    const classroom = this.managing();
    if (!classroom) return;
    void this.everyoneState.run(
      async () => {
        await this.admin.enroll(classroom.id, student.id);
        await this.rereadRoster(classroom.id);
      },
      `${this.studentName(student)} enrolled in ${classroom.name}.`,
    );
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
    const classroom = this.managing();
    if (!student || !classroom) return;
    void this.rosterState.run(async () => {
      await this.admin.unenroll(classroom.id, student.id);
      this.closeRemove();
      await this.rereadRoster(classroom.id);
    }, 'Student removed from the classroom.');
  }
}
