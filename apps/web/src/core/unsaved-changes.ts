import { DestroyRef, Injectable, inject } from '@angular/core';

/* sections report whether they hold work the user hasn't saved: a dirty form, or an answer still streaming. */
@Injectable({ providedIn: 'root' })
export class UnsavedChanges {
  private readonly sources = new Set<() => boolean>();

  /* registers `isunsaved` until the calling component is destroyed. */
  track(isUnsaved: () => boolean): void {
    this.sources.add(isUnsaved);
    inject(DestroyRef).onDestroy(() => this.sources.delete(isUnsaved));
  }

  any(): boolean {
    return [...this.sources].some((isUnsaved) => isUnsaved());
  }
}
