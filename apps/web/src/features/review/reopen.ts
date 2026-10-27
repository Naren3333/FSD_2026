import { Component, EventEmitter, Input, Output, ViewChild, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms';
import { GradingApi } from '../../api/clients';
import type { Evaluation } from '../../api/models';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { MarksPipe } from '../../shared/marks';
import { requiredText } from '../../shared/validators';

/* the reopened grade, and the classroom and assessment shown when it was asked for. */
export interface Reopened {
  result: Evaluation;
  classroomId: string;
  assessmentId: string;
}

/* "reopen to correct" for a finalized grade being read, with its confirmation dialog. */
@Component({
  selector: 'app-review-reopen',
  standalone: true,
  imports: [ReactiveFormsModule, FieldComponent, FieldControlDirective, ConfirmDialogComponent, MarksPipe],
  templateUrl: './reopen.html',
  // the button sits in the panel's button row as its own item.
  host: { style: 'display: contents' },
})
export class ReopenComponent {
  private readonly grading = inject(GradingApi);
  /* the finalized grade being read. */
  @Input({ required: true }) grade!: Evaluation;
  /* the student's name, or their id when not on the roster. */
  @Input({ required: true }) student = '';
  @Input({ required: true }) title = '';
  @Input({ required: true }) classroomId = '';
  @Input({ required: true }) assessmentId = '';
  /* the page's own action is running. */
  @Input() busy = false;
  @Output() readonly reopened = new EventEmitter<Reopened>();
  /* reopening a finalized grade, in its confirmation dialog. */
  readonly reopenState = new ActionState();
  readonly reopening = signal(false);
  readonly reopenForm = new FormGroup({
    reason: new FormControl('', { nonNullable: true, validators: requiredText(1000) }),
  });
  @ViewChild('reopenDialog') reopenDialog?: ConfirmDialogComponent;

  askReopen(): void {
    if (!this.grade) return;
    this.reopenForm.reset();
    this.reopening.set(true);
    this.reopenDialog?.show();
  }

  closeReopen(): void {
    this.reopenForm.reset();
    this.reopening.set(false);
    this.reopenDialog?.close();
  }

  /* reopens the finalized grade being read; the page opens it for marking. */
  reopen(): void {
    const grade = this.grade;
    if (!grade || !this.reopenState.validate(this.reopenForm, 'reopen-form')) return;
    const classroomId = this.classroomId;
    const assessmentId = this.assessmentId;
    void this.reopenState.run(async () => {
      const result = await this.grading.reopen(grade.id, this.reopenForm.controls.reason.value);
      this.closeReopen();
      this.reopened.emit({ result, classroomId, assessmentId });
    });
  }
}
