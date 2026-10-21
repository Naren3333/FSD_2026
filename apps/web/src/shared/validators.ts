import { ValidatorFn, Validators } from '@angular/forms';

/* a required text field of at most `max` characters that is more than whitespace. */
export function requiredText(max: number): ValidatorFn[] {
  return [Validators.required, Validators.maxLength(max), Validators.pattern(/\S/)];
}
