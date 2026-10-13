import { DestroyRef, Injectable, inject } from '@angular/core';

/** Sections report whether they hold work the user hasn't saved: a dirty form, or an answer still streaming. The
 * shell asks before it changes classroom, signs out or lets the page unload, without knowing any section's forms. */
@Injectable({ providedIn: 'root' })
export class UnsavedChanges {
  private readonly sources = new Set<() => boolean>();

  /** Registers `isUnsaved` until the calling component is destroyed. Call from an injection context. */
  track(isUnsaved: () => boolean): void {
    this.sources.add(isUnsaved);
    inject(DestroyRef).onDestroy(() => this.sources.delete(isUnsaved));
  }

  any(): boolean {
    return [...this.sources].some((isUnsaved) => isUnsaved());
  }
}
