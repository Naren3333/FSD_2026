import { Component, EventEmitter, Input, Output, ViewChild, inject, signal } from '@angular/core';
import { GradingApi } from '../../api/clients';
import type { ReleasePolicy } from '../../api/models';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';

/* what an action on the whole assessment did, and the classroom and assessment it was asked for. */
export interface AssessmentActionDone {
  message: string;
  classroomId: string;
  assessmentId: string;
}

/* the chosen assessment's release and finalize-all banners, with their confirmation dialogs. */
@Component({
  selector: 'app-review-assessment-actions',
  standalone: true,
  imports: [ConfirmDialogComponent],
  templateUrl: './assessment-actions.html',
  // the banners sit in the page's filter row as its own items.
  host: { style: 'display: contents' },
})
export class AssessmentActionsComponent {
  private readonly grading = inject(GradingApi);
  @Input({ required: true }) classroomId = '';
  @Input({ required: true }) assessmentId = '';
  /* finalized grades students cannot see yet, across every page. */
  @Input({ required: true }) unreleased = 0;
  /* drafts with nothing left to mark, across every page. */
  @Input({ required: true }) ready = 0;
  @Input({ required: true }) releasePolicy: ReleasePolicy | undefined;
  @Input({ required: true }) title: string | undefined;
  /* whether the page has marks not saved yet; it says so itself. */
  @Input({ required: true }) blocked!: () => boolean;
  /* the page's own action is running. */
  @Input() busy = false;
  @Output() readonly done = new EventEmitter<AssessmentActionDone>();
  /* drafts were finalized, so one open for marking may be final now. */
  @Output() readonly finalized = new EventEmitter<void>();
  /* releasing an assessment's results, in its confirmation dialog. */
  readonly releaseState = new ActionState();
  /* finalizing an assessment's fully marked drafts, in its confirmation dialog. */
  readonly bulkState = new ActionState();
  readonly releasing = signal(false);
  readonly bulkFinalizing = signal(false);
  @ViewChild('releaseDialog') releaseDialog?: ConfirmDialogComponent;
  @ViewChild('bulkDialog') bulkDialog?: ConfirmDialogComponent;

  // --- releasing
  // -------------------------------------------------------------------------------------------

  askRelease(): void {
    this.releasing.set(true);
    this.releaseDialog?.show();
  }

  closeRelease(): void {
    this.releasing.set(false);
    this.releaseDialog?.close();
  }

  release(): void {
    const assessmentId = this.assessmentId;
    const classroomId = this.classroomId;
    if (!assessmentId) return;
    void this.releaseState.run(async () => {
      const result = await this.grading.release(assessmentId);
      this.closeRelease();
      // released, so the page says so at once, after the dialog has closed.
      this.done.emit({
        message: `${result.released} ${result.released === 1 ? 'result' : 'results'} released to students.`,
        classroomId,
        assessmentId,
      });
    });
  }

  // --- finalizing every fully marked grade
  // -------------------------------------------------------------------------------------------

  askFinalizeAll(): void {
    if (this.blocked()) return;
    this.bulkFinalizing.set(true);
    this.bulkDialog?.show();
  }

  closeFinalizeAll(): void {
    this.bulkFinalizing.set(false);
    this.bulkDialog?.close();
  }

  finalizeAll(): void {
    const assessmentId = this.assessmentId;
    const classroomId = this.classroomId;
    if (!assessmentId) return;
    void this.bulkState.run(async () => {
      const result = await this.grading.finalizeAll(assessmentId);
      this.closeFinalizeAll();
      this.finalized.emit();
      const waiting = result.needs_marking
        ? ` ${result.needs_marking} still ${result.needs_marking === 1 ? 'needs' : 'need'} marking.`
        : '';
      this.done.emit({
        message: `${result.finalized} ${result.finalized === 1 ? 'grade' : 'grades'} finalized.${waiting}`,
        classroomId,
        assessmentId,
      });
    });
  }

  /* whether the chosen assessment's grades wait for a release after finalizing. */
  releasesByHand(): boolean {
    return this.releasePolicy === 'manual';
  }

  assessmentTitle(): string {
    return this.title ?? 'this assessment';
  }
}
