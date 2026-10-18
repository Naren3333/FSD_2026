import { Component, ViewChild, computed, inject } from '@angular/core';
import { IdentityApi } from '../../api/clients';
import { ClassroomContext } from '../../core/classroom-context';
import { Session } from '../../core/session';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { StatusComponent } from '../../shared/status';

/** The signed-in user's own account: who they are, and deleting the account for good (ADR 0011). */
@Component({
  selector: 'app-profile',
  standalone: true,
  imports: [ConfirmDialogComponent, StatusComponent],
  templateUrl: './profile.html',
})
export class ProfileComponent {
  readonly session = inject(Session);
  private readonly identity = inject(IdentityApi);
  private readonly context = inject(ClassroomContext);
  private readonly unsaved = inject(UnsavedChanges);
  readonly profile = this.session.profile;
  readonly state = new ActionState();
  /** A teacher's own classrooms: deleting those comes first, since each one deletes its students' work. */
  readonly classroomCount = computed(() => (this.session.isTeacher() ? this.context.classrooms().length : 0));
  @ViewChild('deleteDialog') deleteDialog?: ConfirmDialogComponent;

  confirmDelete(): void {
    if (this.unsaved.any()) {
      this.state.error.set('Finish or clear your unsaved forms before deleting your account.');
      return;
    }
    this.deleteDialog?.show();
  }

  closeDelete(): void {
    this.deleteDialog?.close();
  }

  /** Deletes the account, then signs out: the account cannot sign in again. */
  deleteAccount(): void {
    void this.state.run(async () => {
      await this.identity.deleteAccount();
      await this.session.logout();
    });
  }
}
