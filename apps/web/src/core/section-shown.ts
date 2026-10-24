import { DestroyRef, inject } from '@angular/core';
import { NavigationEnd, Router } from '@angular/router';
import { filter } from 'rxjs';
import type { Section } from '../shared/section';

/* the first path segment of a url: the section it opens. */
export function sectionOf(url: string): string {
  return url.split(/[?#]/)[0].split('/').find(Boolean) ?? '';
}

/* runs `callback` when the user comes back to the kept-alive section `path` from another one, at most once per `minAgeMs`; call in a component's injection context. */
export function onSectionShown(path: string, callback: () => void, minAgeMs = 20_000): void {
  const router = inject(Router);
  // the page is created by the navigation to its own section, which is not a return.
  let previous = path;
  let last = Date.now();
  const subscription = router.events
    .pipe(filter((event): event is NavigationEnd => event instanceof NavigationEnd))
    .subscribe((event) => {
      const section = sectionOf(event.urlAfterRedirects);
      const returned = section === path && previous !== path;
      previous = section;
      if (!returned || Date.now() - last < minAgeMs) return;
      last = Date.now();
      callback();
    });
  inject(DestroyRef).onDestroy(() => subscription.unsubscribe());
}

/* re-reads a loaded section in place, keeping it on screen; one that failed keeps its try again instead. */
export function rereadQuietly<T>(section: Section<T>, work: () => Promise<T>): void {
  if (section.error() || section.loading() || section.value() === null) return;
  void section.poll(work);
}
