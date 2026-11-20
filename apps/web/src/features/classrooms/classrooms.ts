import { Component, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import type { Classroom } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagerComponent, lastPage, pageItems } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { StatusComponent } from '../../shared/status';

/* the teacher's classrooms, which the school's admin creates and assigns (adr 0019): open one or see its students. */
@Component({
  selector: 'app-classrooms',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    StatusComponent,
    PagerComponent,
    NoClassroomComponent,
    RouterLink,
  ],
  templateUrl: './classrooms.html',
})
export class ClassroomsComponent {
  readonly session = inject(Session);
  private readonly context = inject(ClassroomContext);
  private readonly unsaved = inject(UnsavedChanges);
  private readonly router = inject(Router);
  readonly classrooms = this.context.classrooms;
  readonly roomPage = signal(0);
  readonly roomPageSize = 20;
  readonly effectiveRoomPage = computed(() =>
    Math.min(this.roomPage(), lastPage(this.classrooms().length, this.roomPageSize)),
  );
  readonly visibleRooms = computed(() =>
    pageItems(this.classrooms(), this.effectiveRoomPage(), this.roomPageSize),
  );
  readonly classroomId = this.context.classroomId;
  readonly classroomName = this.context.classroomName;
  readonly state = new ActionState();

  /* whether a form here or elsewhere holds unsaved work, which keeps the classroom from changing. */
  classroomLocked(): boolean {
    return this.unsaved.locksClassroom();
  }

  /* opens a classroom's dashboard, choosing it first when it isn't the current one. */
  open(classroom: Classroom): void {
    if (classroom.id !== this.classroomId()) {
      if (this.classroomLocked()) return;
      this.context.select(classroom.id);
    }
    void this.router.navigateByUrl('/dashboard');
  }

  refresh(): void {
    void this.state.run(() => this.context.refresh());
  }
}
