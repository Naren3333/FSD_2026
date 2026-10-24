import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { IdentityApi } from '../../api/clients';
import type { Student, StudentPage } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { studentName } from '../../shared/names';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';

/* the teacher's students: the current classroom's roster, and everyone in the school to enroll from. */
@Component({
  selector: 'app-students',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    FieldComponent,
    FieldControlDirective,
    IconComponent,
    StatusComponent,
    SectionStateComponent,
    PagerComponent,
    ConfirmDialogComponent,
    NoClassroomComponent,
  ],
  templateUrl: './students.html',
})
export class StudentsComponent {
  readonly session = inject(Session);
  private readonly context = inject(ClassroomContext);
  private readonly identity = inject(IdentityApi);
  readonly classroomId = this.context.classroomId;
  readonly classroomName = this.context.classroomName;
  readonly rosterState = new ActionState();
  /* the page of the classroom's students on show, by name, and how many are enrolled. */
  readonly roster = new Section<StudentPage>();
  readonly rosterPage = signal(0);
  readonly rosterPageSize = 20;
  /* every enrolled student's id, however many pages the roster has, to mark them in the school list. */
  readonly enrolledIds = new Section<string[]>();
  /* the page of school students on show, fetched from the server ten at a time, and how many match the search across every page. */
  readonly everyone = new Section<StudentPage>();
  readonly enrolled = computed(() => new Set(this.enrolledIds.value() ?? []));
  readonly everyoneState = new ActionState();
  /* what the teacher typed to narrow the list, matched by the server against names and usernames. */
  readonly search = signal('');
  readonly page = signal(0);
  readonly pageSize = 10;
  private searchTimer?: ReturnType<typeof setTimeout>;
  /* the student the remove dialog is asking about. */
  readonly removing = signal<Student | null>(null);
  readonly studentName = studentName;
  @ViewChild('removeDialog') removeDialog?: ConfirmDialogComponent;
  /* the line under the title: how many are enrolled once the roster is known. */
  readonly subtitle = computed(() => {
    if (!this.classroomId()) return 'Choose a classroom to see its students.';
    const name = this.classroomName() || 'this classroom';
    const total = this.roster.ready() ? this.roster.value()?.total : undefined;
    return total === undefined ? `Students in ${name}` : `${total} enrolled in ${name}`;
  });

  constructor() {
    onClassroomChange(() => {
      this.rosterPage.set(0);
      this.loadRoster();
    });
    this.loadEveryone();
    // students who signed in or were enrolled elsewhere since show up on return.
    onSectionShown('students', () => {
      if (!this.session.isTeacher()) return;
      rereadQuietly(this.everyone, () => this.readEveryone(() => true));
      const classroomId = this.classroomId();
      if (!classroomId) return;
      const same = () => this.classroomId() === classroomId;
      rereadQuietly(this.roster, () => this.readRoster(classroomId, same));
      rereadQuietly(this.enrolledIds, () => this.identity.rosterIds(classroomId));
    });
  }

  /* narrows the list and goes back to its first page once the teacher pauses typing. */
  searchFor(words: string): void {
    this.search.set(words);
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => {
      this.page.set(0);
      this.loadEveryone();
    }, 250);
  }

  loadEveryone(): void {
    if (!this.session.isTeacher()) return;
    void this.everyone.load((current) => this.readEveryone(current));
  }

  /* fetches the page on show; a page past the end, after students leave, falls back to the last one. */
  private async readEveryone(current: () => boolean): Promise<StudentPage> {
    const search = this.search().trim();
    let result = await this.identity.students(search, this.page() * this.pageSize, this.pageSize);
    if (!result.students.length && result.total && this.page() > 0) {
      const last = Math.ceil(result.total / this.pageSize) - 1;
      result = await this.identity.students(search, last * this.pageSize, this.pageSize);
      if (current()) this.page.set(last);
    }
    return result;
  }

  /* enrolls a student from the school list into the current classroom. */
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

  /* the current classroom's students, for its teacher: the page on show and every enrolled id. */
  loadRoster(): void {
    const classroomId = this.classroomId();
    this.roster.clear();
    this.enrolledIds.clear();
    if (!classroomId || !this.session.isTeacher()) return;
    void this.roster.load((current) => this.readRoster(classroomId, current));
    void this.enrolledIds.load(() => this.identity.rosterIds(classroomId));
  }

  /* shows the roster page the pager moved to. */
  showRosterPage(): void {
    const classroomId = this.classroomId();
    if (classroomId) void this.roster.load((current) => this.readRoster(classroomId, current));
  }

  /* the roster page on show; a page past the end, after students leave, falls back to the last one. */
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

  /* re-reads the roster page and the enrolled ids after a student joined or left. */
  private async rereadRoster(classroomId: string): Promise<void> {
    const same = () => this.classroomId() === classroomId;
    await Promise.all([
      this.roster.reload(() => this.readRoster(classroomId, same)),
      this.enrolledIds.reload(() => this.identity.rosterIds(classroomId)),
    ]);
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
}
