import '@angular/compiler';
import { describe, it, expect } from 'vitest';
import { dollars, resetTime } from './usage';

describe('AI use meter', () => {
  it('shows dollars to the cent and never hides a real charge as zero', () => {
    expect(dollars(1.234)).toBe('$1.23');
    expect(dollars(5)).toBe('$5.00');
    expect(dollars(0)).toBe('$0.00');
    expect(dollars(0.0004)).toBe('under $0.01');
  });
  it('names the reset day only when it is not today', () => {
    const now = new Date(2026, 9, 13, 15, 0);
    expect(resetTime(new Date(2026, 9, 13, 23, 30).toISOString(), now)).not.toContain('Oct');
    expect(resetTime(new Date(2026, 9, 14, 0, 0).toISOString(), now)).toContain('14 Oct');
  });
});
