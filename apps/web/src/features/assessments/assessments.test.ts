import { describe, it, expect } from 'vitest';
import { optionLines } from './assessments';

describe('optionLines', () => {
  it('takes one trimmed option per non-blank line', () => {
    expect(optionLines(' 1/2 \n\n0.5\r\n  \n50%')).toEqual(['1/2', '0.5', '50%']);
  });
});
