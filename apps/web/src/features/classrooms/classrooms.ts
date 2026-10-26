import { Component, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { Router, RouterLink } from '@angular/router';
import { IdentityApi } from '../../api/clients';
import type { Classroom } from '../../api/models';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { IconComponent } from '../../shared/icon';
import { NoClassroomComponent } from '../../shared/no-classroom';
import { PagerComponent } from '../../shared/pager';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';
import { PageHeaderComponent } from '../../shared/page-header';
import { RefreshButtonComponent } from '../../shared/refresh-button';

@Component({
  selector: 'app-classrooms',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    PagerComponent,
    ConfirmDialogComponent,
    NoClassroomComponent,
    IconComponent,
    RouterLink,
  ],
  templateUrl: './classrooms.html',
})
export class ClassroomsComponent {
  readonly session = inject(Session);
  private readonly context = inject(ClassroomContext);
  private readonly identity = inject(IdentityApi);
  private readonly unsaved = inject(UnsavedChanges);
  private readonly router = inject(Router);
  readonly classrooms = this.context.classrooms;
  readonly roomPage = signal(0);
  readonly roomPageSize = 20;
  readonly effectiveRoomPage = computed(() => {
    const last = Math.max(0, Math.ceil(this.classrooms().length / this.roomPageSize) - 1);
    return Math.min(this.roomPage(), last);
  });
  readonly visibleRooms = computed(() => {
    const page = this.effectiveRoomPage();
    return this.classrooms().slice(page * this.roomPageSize, (page + 1) * this.roomPageSize);
  });
  readonly classroomId = this.context.classroomId;
  readonly classroomName = this.context.classroomName;
  readonly state = new ActionState();
  /* the classroom the delete dialog is asking about. */
  readonly deleting = signal<Classroom | null>(null);
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;
  readonly form = new FormGroup({
    name: new FormControl('', { nonNullable: true, validators: requiredText(120) }),
  });

  constructor() {
    this.unsaved.track(() => this.form.dirty);
  }

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

  create(): void {
    if (!this.state.validate(this.form, 'classroom-form')) return;
    void this.state.run(async () => {
      await this.identity.createClassroom(this.form.controls.name.value);
      this.form.reset();
      await this.context.refresh();
    }, 'Classroom created.');
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
