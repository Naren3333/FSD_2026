import { Component, Input, signal } from '@angular/core';
import { FormGroup } from '@angular/forms';

export class ActionState {
  readonly busy = signal(false);
  readonly error = signal('');
  readonly message = signal('');
  async run(action: () => Promise<void>, success=''): Promise<void> {
    if (this.busy()) return;
    this.busy.set(true); this.error.set(''); this.message.set('');
    try { await action(); this.message.set(success); } catch(error) { this.error.set(error instanceof Error ? error.message : 'The operation failed. Retry later.'); }
    finally { this.busy.set(false); }
  }
  validate(form: FormGroup): boolean {
    form.markAllAsTouched();
    if (form.invalid) { this.error.set('Check the highlighted fields before continuing.'); setTimeout(() => document.querySelector<HTMLElement>('form:not([hidden]) .ng-invalid:not(form)')?.focus()); return false; }
    return true;
  }
}

@Component({selector:'app-status',standalone:true,template:`<div class="feedback" [attr.aria-busy]="state.busy()"><p role="status" aria-live="polite">{{state.busy() ? 'Working…' : state.message()}}</p>@if(state.error()){<p role="alert" class="error">{{state.error()}}</p>}</div>`})
export class StatusComponent { @Input({required:true}) state!: ActionState; }

@Component({selector:'app-field',standalone:true,template:`<label [for]="controlId">{{label}}</label><ng-content/><small [id]="controlId+'-help'" [class.error]="invalid">{{invalid ? error : hint}}</small>`})
export class FieldComponent {
  @Input({required:true}) controlId=''; @Input({required:true}) label=''; @Input() hint=''; @Input() error='This field is required.'; @Input() invalid=false;
}

