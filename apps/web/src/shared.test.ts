import '@angular/compiler';
import { describe, it, expect } from 'vitest';
import { ActionState } from './shared';
describe('ActionState',()=>{
  it('prevents duplicate mutations and reports a failure',async()=>{const state=new ActionState();let calls=0;let finish:()=>void=()=>{};const first=state.run(async()=>{calls++;await new Promise<void>(resolve=>finish=resolve);throw new Error('Unavailable');});await state.run(async()=>{calls++;});expect(calls).toBe(1);finish();await first;expect(state.error()).toBe('Unavailable');expect(state.busy()).toBe(false);});
  it('only announces success after the operation completes',async()=>{const state=new ActionState();await state.run(async()=>{},'Saved');expect(state.message()).toBe('Saved');expect(state.error()).toBe('');});
});

