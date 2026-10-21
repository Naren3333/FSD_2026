import { Component, ElementRef, EventEmitter, Input, Output, ViewChild, signal } from '@angular/core';
import { ActionState } from './action-state';
import { StatusComponent } from './status';

/* the app-owned confirmation dialog: a question, what it means (and any fields the action needs, in the page's own form), cancel, focused first, and the action. */
@Component({
  selector: 'app-confirm-dialog',
  standalone: true,
  imports: [StatusComponent],
  templateUrl: './confirm-dialog.html',
})
export class ConfirmDialogComponent {
  /* prefixes the title's id, such as "remove" for "remove-title". */
  @Input({ required: true }) dialogId = '';
  @Input({ required: true }) heading = '';
  @Input({ required: true }) confirmLabel = '';
  /* the action's state: it disables the buttons while it runs and is announced inside the dialog. */
  @Input({ required: true }) state!: ActionState;
  @Output() readonly confirm = new EventEmitter<void>();
  /* however the dialog closed: cancel, escape (not while the action runs) or the page closing it. */
  @Output() readonly closed = new EventEmitter<void>();
  @ViewChild('dialog', { static: true }) private dialog!: ElementRef<HTMLDialogElement>;
  readonly isOpen = signal(false);

  /* opens the dialog without the action's earlier message or error. */
  show(): void {
    this.state.error.set('');
    this.state.message.set('');
    this.isOpen.set(true);
    this.dialog.nativeElement.showModal();
  }

  close(): void {
    this.dialog.nativeElement.close();
  }

  /* escape would close the dialog while its action is still running; it waits until the action ends. */
  onCancel(event: Event): void {
    if (this.state.busy()) event.preventDefault();
  }

  onClose(): void {
    this.isOpen.set(false);
    this.closed.emit();
  }
}
