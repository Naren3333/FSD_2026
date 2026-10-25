import { Component, EventEmitter, Input, Output } from '@angular/core';
import { IconComponent } from './icon';

/* an icon-only refresh button named by its label, such as "refresh assessments"; the icon spins, and the button is disabled, while busy. */
@Component({
  selector: 'app-refresh-button',
  standalone: true,
  imports: [IconComponent],
  template: `
    <button
      type="button"
      class="icon-button secondary"
      [class.busy]="busy"
      [attr.aria-label]="label"
      [title]="label"
      [disabled]="disabled || busy"
      (click)="refresh.emit()"
    >
      <app-icon name="refresh" />
    </button>
  `,
})
export class RefreshButtonComponent {
  @Input({ required: true }) label = '';
  @Input() disabled = false;
  @Input() busy = false;
  @Output() readonly refresh = new EventEmitter<void>();
}
