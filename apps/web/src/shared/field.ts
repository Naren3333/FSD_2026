import { Component, Directive, Input, inject } from '@angular/core';
import type { AbstractControl } from '@angular/forms';

/** A labelled form control with its hint, or its error once invalid. Mark the control (the element whose id is
 * `controlId`) with `appFieldControl`. */
@Component({ selector: 'app-field', standalone: true, templateUrl: './field.html' })
export class FieldComponent {
  @Input({ required: true }) controlId = '';
  @Input({ required: true }) label = '';
  @Input() hint = '';
  @Input() error = 'This field is required.';
  @Input() invalid = false;
  /** The form control, whose error shows once it is touched and invalid; or set `invalid` instead. */
  @Input() control: AbstractControl | null | undefined = null;

  /** Whether the field shows its error. */
  get showsError(): boolean {
    return this.invalid || (!!this.control?.touched && this.control.invalid);
  }
}

/** The control inside an app-field: aria-invalid while the field shows its error, and described by its hint or
 * error. */
@Directive({
  selector: '[appFieldControl]',
  standalone: true,
  host: {
    '[attr.aria-invalid]': 'field.showsError || null',
    '[attr.aria-describedby]': "field.showsError || field.hint ? field.controlId + '-help' : null",
  },
})
export class FieldControlDirective {
  readonly field = inject(FieldComponent);
}
