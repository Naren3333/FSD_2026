/** The message to show for a failure: the error's own message when it has one, else `fallback`. */
export function messageOf(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}
