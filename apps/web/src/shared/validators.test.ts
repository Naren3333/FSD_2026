import { FormControl } from '@angular/forms';
import { describe, expect, it } from 'vitest';
import { requiredText } from './validators';

describe('requiredText', () => {
  const control = (value: string) => new FormControl(value, { validators: requiredText(5) });

  it('rejects an empty or whitespace-only value', () => {
    expect(control('').hasError('required')).toBe(true);
    expect(control('   ').hasError('pattern')).toBe(true);
    expect(control(' \n\t ').valid).toBe(false);
  });

  it('accepts text up to the limit, surrounding spaces included, and rejects longer text', () => {
    expect(control(' Quiz').valid).toBe(true);
    expect(control('Quizzes').hasError('maxlength')).toBe(true);
  });
});
