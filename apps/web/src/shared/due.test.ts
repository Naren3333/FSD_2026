import { describe, expect, it } from 'vitest';
import { dueState } from './due';

describe('dueState', () => {
  it('says where a student stands with the due date', () => {
    const now = new Date('2026-10-16T00:00:00Z');
    expect(dueState(null, 'reject', now)).toBe('none');
    expect(dueState('2026-10-17T00:00:00Z', 'reject', now)).toBe('upcoming');
    expect(dueState('2026-10-15T00:00:00Z', 'accept_flagged', now)).toBe('late');
    expect(dueState('2026-10-15T00:00:00Z', 'reject', now)).toBe('closed');
  });
});
