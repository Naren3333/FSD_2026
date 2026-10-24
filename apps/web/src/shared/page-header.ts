import { Component, Input } from '@angular/core';

/* a page's title, one short line under it, and its actions on the right (wrapping under the title when narrow). */
@Component({ selector: 'app-page-header', standalone: true, templateUrl: './page-header.html' })
export class PageHeaderComponent {
  @Input({ required: true }) title = '';
  @Input() subtitle = '';
  /* the heading's id, for a section that the heading names; the page can then move focus to it. */
  @Input() headingId = '';
}
