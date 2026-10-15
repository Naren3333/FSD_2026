import { Component, EventEmitter, Input, Output } from '@angular/core';

/** How many pages `total` items fill, `pageSize` at a time; always at least one. */
export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize));
}

/** Previous and Next controls for a list the server pages, with the page on show and how many items match. */
@Component({ selector: 'app-pager', standalone: true, templateUrl: './pager.html' })
export class PagerComponent {
  /** Names the navigation landmark, such as "All students pages". */
  @Input({ required: true }) label = '';
  /** The page on show, counted from 0. */
  @Input({ required: true }) page = 0;
  @Input({ required: true }) pageSize = 1;
  @Input({ required: true }) total = 0;
  /** What the items are called in the count, such as "students". */
  @Input() noun = 'students';
  @Input() disabled = false;
  @Output() readonly go = new EventEmitter<number>();

  get pages(): number {
    return pageCount(this.total, this.pageSize);
  }
}
