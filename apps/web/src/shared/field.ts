import { Component, Input } from '@angular/core';

/** A labelled form control with its hint, or its error once invalid. */
@Component({ selector: 'app-field', standalone: true, templateUrl: './field.html' })
export class FieldComponent {
  @Input({ required: true }) controlId = '';
  @Input({ required: true }) label = '';
  @Input() hint = '';
  @Input() error = 'This field is required.';
  @Input() invalid = false;
}
