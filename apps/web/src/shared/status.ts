import { Component, Input } from '@angular/core';
import { ActionState } from './action-state';

/* announces an actionstate: "working…", its success message, or its error. */
@Component({ selector: 'app-status', standalone: true, templateUrl: './status.html' })
export class StatusComponent {
  @Input({ required: true }) state!: ActionState;
}
