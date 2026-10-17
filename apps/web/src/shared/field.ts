import { AfterViewChecked, Component, ElementRef, Input, inject } from '@angular/core';

/** A labelled form control with its hint, or its error once invalid. The control (the element whose id is
 * `controlId`) is marked aria-invalid while the error shows, and described by the hint or error. */
@Component({ selector: 'app-field', standalone: true, templateUrl: './field.html' })
export class FieldComponent implements AfterViewChecked {
  @Input({ required: true }) controlId = '';
  @Input({ required: true }) label = '';
  @Input() hint = '';
  @Input() error = 'This field is required.';
  @Input() invalid = false;
  private readonly host: HTMLElement = inject(ElementRef).nativeElement;

  /** The projected control can appear or change after this component's own inputs do, so it is checked each time. */
  ngAfterViewChecked(): void {
    const control = this.host.querySelector(`[id="${this.controlId}"]`);
    if (!control) return;
    if (this.invalid) control.setAttribute('aria-invalid', 'true');
    else control.removeAttribute('aria-invalid');
    const help = `${this.controlId}-help`;
    const described = (control.getAttribute('aria-describedby') ?? '').split(/\s+/).filter(Boolean);
    if ((this.invalid || this.hint) && !described.includes(help))
      control.setAttribute('aria-describedby', [...described, help].join(' '));
  }
}
