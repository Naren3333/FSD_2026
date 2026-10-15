import { Component, Input, Signal } from '@angular/core';

/** A section's loading message while it loads, or its error once it has failed; nothing once it is ready. */
@Component({ selector: 'app-section-state', standalone: true, templateUrl: './section-state.html' })
export class SectionStateComponent {
  @Input({ required: true }) section!: { loading: Signal<boolean>; error: Signal<string> };
  /** Such as "Loading assessments…". */
  @Input({ required: true }) loading = '';
}
