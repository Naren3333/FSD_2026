import { Component, EventEmitter, Input, Output } from '@angular/core';

/* how many pages `total` items fill, `pagesize` at a time; always at least one. */
export function pageCount(total: number, pageSize: number): number {
  return Math.max(1, Math.ceil(total / pageSize));
}

/* previous and next controls for a list the server pages, with the page on show and how many items match. */
@Component({ selector: 'app-pager', standalone: true, templateUrl: './pager.html' })
export class PagerComponent {
  /* names the navigation landmark, such as "all students pages". */
  @Input({ required: true }) label = '';
  /* the page on show, counted from 0; bind it two-way, `[(page)]`. */
  @Input({ required: true }) page = 0;
  @Output() readonly pageChange = new EventEmitter<number>();
  @Input({ required: true }) pageSize = 1;
  @Input({ required: true }) total = 0;
  /* what the items are called in the count, such as "students". */
  @Input() noun = 'students';
  @Input() disabled = false;
  @Output() readonly go = new EventEmitter<void>();

  get pages(): number {
    return pageCount(this.total, this.pageSize);
  }

  move(by: number): void {
    this.page = Math.min(this.pages - 1, Math.max(0, this.page + by));
    this.pageChange.emit(this.page);
    this.go.emit();
  }
}
