import { signal } from '@angular/core';
import { FormGroup } from '@angular/forms';
import { messageOf } from './errors';

/* the busy flag, success message and error of one section's actions. */
export class ActionState {
  readonly busy = signal(false);
  readonly error = signal('');
  readonly message = signal('');
  async run(action: () => Promise<void>, success = ''): Promise<void> {
    if (this.busy()) return;
    this.busy.set(true);
    this.error.set('');
    this.message.set('');
    try {
      await action();
      this.message.set(success);
    } catch (error) {
      this.error.set(messageOf(error, 'The operation failed. Retry later.'));
    } finally {
      this.busy.set(false);
    }
  }
  /* shows the form's errors and, when it is invalid, focuses its first invalid control: the one inside the element with id `formid`, never another form on the page. */
  validate(form: FormGroup, formId: string): boolean {
    form.markAllAsTouched();
    if (form.invalid) {
      this.error.set('Check the highlighted fields before continuing.');
      setTimeout(() =>
        Array.from(
          document
            .getElementById(formId)
            ?.querySelectorAll<HTMLElement>(':is(input, select, textarea).ng-invalid') ?? [],
        )
          .find((el) => !el.closest('[hidden]'))
          ?.focus(),
      );
      return false;
    }
    return true;
  }
}
