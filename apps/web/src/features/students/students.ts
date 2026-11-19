import { Component, computed, inject, signal } from '@angular/core';
import { IdentityApi } from '../../api/clients';
import type { StudentPage } from '../../api/models';
import { ClassroomContext, onClassroomChange } from '../../core/classroom-context';
import { onSectionShown, rereadQuietly } from '../../core/section-shown';
import { Session } from '../../core/session';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { studentName } from '../../shared/names';
import { PageHeaderComponent } from '../../shared/page-header';
import { readPage } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';

/* the teacher's students: the current classroom's roster, which the school's admin keeps (adr 0019). */
@Component({
  selector: 'app-students',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    SectionStateComponent,
    PagerComponent,
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
  /* the page of the classroom's students on show, by name, and how many are enrolled. */
  readonly roster = new Section<StudentPage>();
  readonly rosterPage = signal(0);
  readonly rosterPageSize = 20;
  readonly studentName = studentName;
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
    // students the admin enrolled or removed since show up on return.
    onSectionShown('students', () => {
      const classroomId = this.classroomId();
      if (!classroomId || !this.session.isTeacher()) return;
      rereadQuietly(this.roster, () =>
        this.readRoster(classroomId, () => this.classroomId() === classroomId),
      );
    });
  }

  /* the current classroom's students, for its teacher. */
  loadRoster(): void {
    const classroomId = this.classroomId();
    this.roster.clear();
    if (!classroomId || !this.session.isTeacher()) return;
    void this.roster.load((current) => this.readRoster(classroomId, current));
  }

  /* shows the roster page the pager moved to. */
  showRosterPage(): void {
    const classroomId = this.classroomId();
    if (classroomId) void this.roster.load((current) => this.readRoster(classroomId, current));
  }

  /* the roster page on show; a page past the end, after students leave, falls back to the last one. */
  private async readRoster(classroomId: string, current: () => boolean): Promise<StudentPage> {
    const { result, page } = await readPage(
      this.rosterPage(),
      this.rosterPageSize,
      (offset, limit) => this.identity.rosterPage(classroomId, offset, limit),
      (r) => !r.students.length,
    );
    if (current()) this.rosterPage.set(page);
    return result;
  }
}
