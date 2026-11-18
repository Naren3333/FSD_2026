import { DestroyRef, inject, signal } from '@angular/core';

/* a shell's sidebar as the phone drawer; create it in an injection context. */
export class Drawer {
  readonly open = signal(false);

  /* first gets focus when the drawer opens, opener when it closes. */
  constructor(
    private readonly first: () => HTMLElement | undefined,
    private readonly opener: () => HTMLElement | undefined,
  ) {
    // a drawer left open while the window widens would keep the page inert.
    if (typeof matchMedia === 'function') {
      const wide = matchMedia('(min-width: 900px)');
      const close = () => wide.matches && this.open.set(false);
      wide.addEventListener('change', close);
      inject(DestroyRef).onDestroy(() => wide.removeEventListener('change', close));
    }
  }

  show(): void {
    this.open.set(true);
    // focus moves once the drawer is visible.
    setTimeout(() => this.first()?.focus());
  }

  hide(): void {
    this.open.set(false);
    setTimeout(() => this.opener()?.focus());
  }
}
