// @vitest-environment jsdom
import { FormControl, FormGroup, Validators } from '@angular/forms';
import { describe, it, expect } from 'vitest';
import { ActionState } from './action-state';
describe('ActionState', () => {
  it('prevents duplicate mutations and reports a failure', async () => {
    const state = new ActionState();
    let calls = 0;
    let finish: () => void = () => {};
    const first = state.run(async () => {
      calls++;
      await new Promise<void>((resolve) => (finish = resolve));
      throw new Error('Unavailable');
    });
    await state.run(async () => {
      calls++;
    });
    expect(calls).toBe(1);
    finish();
    await first;
    expect(state.error()).toBe('Unavailable');
    expect(state.busy()).toBe(false);
  });
  it('only announces success after the operation completes', async () => {
    const state = new ActionState();
    await state.run(async () => {}, 'Saved');
    expect(state.message()).toBe('Saved');
    expect(state.error()).toBe('');
  });
  it('focuses the first invalid control of the form it validates, not of another form above it', async () => {
    document.body.innerHTML = `
      <form id="preview"><input class="ng-invalid" id="other" /></form>
      <form id="editor"><input class="ng-valid" /><textarea class="ng-invalid" id="mine"></textarea></form>`;
    const state = new ActionState();
    const form = new FormGroup({ title: new FormControl('', Validators.required) });
    expect(state.validate(form, 'editor')).toBe(false);
    await new Promise((resolve) => setTimeout(resolve));
    expect(document.activeElement?.id).toBe('mine');
    expect(form.controls.title.touched).toBe(true);
  });
});
