import { signal } from '@angular/core';
import { FormGroup } from '@angular/forms';

/** The busy flag, success message and error of one section's actions. */
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
      this.error.set(error instanceof Error ? error.message : 'The operation failed. Retry later.');
    } finally {
      this.busy.set(false);
    }
  }
  validate(form: FormGroup): boolean {
    form.markAllAsTouched();
    if (form.invalid) {
      this.error.set('Check the highlighted fields before continuing.');
      setTimeout(() =>
        Array.from(document.querySelectorAll<HTMLElement>('form .ng-invalid:not(form)'))
          .find((el) => !el.closest('[hidden]'))
          ?.focus(),
      );
      return false;
    }
    return true;
  }
}
