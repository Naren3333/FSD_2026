import { Component, inject } from '@angular/core';
import { ClassroomContext } from '../core/classroom-context';

/* wraps a section's "no classroom" message, which shows only once the user's classrooms are known. */
@Component({
  selector: 'app-no-classroom',
  standalone: true,
  template: `
    @if (context.known()) {
      <ng-content />
    } @else if (context.error()) {
      <p role="alert" class="error">Your classrooms could not be loaded. {{ context.error() }}</p>
      <button type="button" class="secondary" (click)="retry()" [disabled]="context.loading()">
        Try again
      </button>
    } @else {
      <p role="status" aria-live="polite">Loading your classrooms…</p>
    }
  `,
})
export class NoClassroomComponent {
  readonly context = inject(ClassroomContext);

  retry(): void {
    // a failure shows here again, from the context's error.
    this.context.refresh().catch(() => undefined);
  }
}
