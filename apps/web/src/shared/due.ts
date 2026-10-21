import type { LatePolicy } from '../api/models';

/* where a student stands with an assessment's due date. */
export type DueState = 'none' | 'upcoming' | 'late' | 'closed';

export function dueState(
  due: string | null | undefined,
  latePolicy: LatePolicy | undefined,
  now = new Date(),
): DueState {
  if (!due) return 'none';
  if (new Date(due) >= now) return 'upcoming';
  return latePolicy === 'reject' ? 'closed' : 'late';
}
