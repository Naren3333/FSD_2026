import { describe, it, expect } from 'vitest';
import type { ActivatedRouteSnapshot, DetachedRouteHandle } from '@angular/router';
import { KeepAliveStrategy } from './keep-alive';

const route = (path: string, keepAlive = true) =>
  ({ routeConfig: { path }, data: { keepAlive } }) as unknown as ActivatedRouteSnapshot;

describe('KeepAliveStrategy', () => {
  it('hands a kept section back when the user returns to it', () => {
    const strategy = new KeepAliveStrategy();
    const handle = {} as DetachedRouteHandle;
    expect(strategy.shouldDetach(route('materials'))).toBe(true);
    strategy.store(route('materials'), handle);
    expect(strategy.shouldAttach(route('materials'))).toBe(true);
    expect(strategy.retrieve(route('materials'))).toBe(handle);
    expect(strategy.shouldAttach(route('chat'))).toBe(false);
  });

  it('keeps only routes that opt in, and forgets a cleared handle', () => {
    const strategy = new KeepAliveStrategy();
    expect(strategy.shouldDetach(route('login', false))).toBe(false);
    strategy.store(route('materials'), {} as DetachedRouteHandle);
    strategy.store(route('materials'), null);
    expect(strategy.shouldAttach(route('materials'))).toBe(false);
  });
});
