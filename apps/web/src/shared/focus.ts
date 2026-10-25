/* focuses the element with `id` once the view has rendered it. */
export function focusLater(id: string): void {
  setTimeout(() => document.getElementById(id)?.focus());
}
