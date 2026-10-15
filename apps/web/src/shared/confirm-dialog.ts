import { Component, ElementRef, EventEmitter, Input, Output, ViewChild, signal } from '@angular/core';
import { ActionState } from './action-state';
import { StatusComponent } from './status';

/** The app-owned confirmation dialog: a question, what it means (and any fields the action needs, in the page's own
 * form), Cancel, focused first, and the action. */
@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [StatusComponent],
  templateUrl: './confirm-dialog.html',
})
export class ConfirmDialogComponent {
  /** Prefixes the title's id, such as "remove" for "remove-title". */
  @Input({ required: true }) dialogId = '';
  @Input({ required: true }) heading = '';
  @Input({ required: true }) confirmLabel = '';
  /** The action's state: it disables the buttons while it runs and is announced inside the dialog. */
  @Input({ required: true }) state!: ActionState;
  @Output() readonly confirm = new EventEmitter<void>();
  /** However the dialog closed: Cancel, Escape or the page closing it. */
  @Output() readonly closed = new EventEmitter<void>();
  @ViewChild('dialog', { static: true }) private dialog!: ElementRef<HTMLDialogElement>;
  readonly isOpen = signal(false);

  show(): void {
    this.isOpen.set(true);
    this.dialog.nativeElement.showModal();
  }

  close(): void {
    this.dialog.nativeElement.close();
  }

  onClose(): void {
    this.isOpen.set(false);
    this.closed.emit();
  }
}
