import { DestroyRef, Injectable, inject } from '@angular/core';

/* sections report whether they hold work the user hasn't saved: a dirty form, or an answer still streaming. */
@Injectable({ providedIn: 'root' })
export class UnsavedChanges {
  private readonly sources = new Map<() => boolean, boolean>();

  /* registers `isUnsaved` until the calling component is destroyed. work that survives a classroom change (a chat draft kept per classroom) passes `locksClassroom: false`: it still warns on unload and sign-out. */
  track(isUnsaved: () => boolean, options: { locksClassroom?: boolean } = {}): void {
    this.sources.set(isUnsaved, options.locksClassroom ?? true);
    inject(DestroyRef).onDestroy(() => this.sources.delete(isUnsaved));
  }

  any(): boolean {
    for (const isUnsaved of this.sources.keys()) if (isUnsaved()) return true;
    return false;
  }

  /* whether unsaved work would be lost by changing classrooms. */
  locksClassroom(): boolean {
    for (const [isUnsaved, locks] of this.sources) if (locks && isUnsaved()) return true;
    return false;
  }
}
