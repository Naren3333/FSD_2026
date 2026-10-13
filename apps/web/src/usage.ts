import { Component, OnInit, inject, signal } from '@angular/core';
import { Api, Usage } from './api';

/** Dollars as the meter shows them: cents, and never "$0.00" for spend that is real but under a cent. */
export function dollars(value: number): string {
  if (value > 0 && value < 0.005) return 'under $0.01';
  return new Intl.NumberFormat('en-SG', {style: 'currency', currency: 'USD', currencyDisplay: 'narrowSymbol'}).format(value);
}

/** When the allowance comes back, in the viewer's local time: today's or tomorrow's date only when it matters. */
export function resetTime(iso: string, now = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString('en-SG', {hour: 'numeric', minute: '2-digit'});
  return at.toDateString() === now.toDateString() ? time : `${time} on ${at.toLocaleDateString('en-SG', {day: 'numeric', month: 'short'})}`;
}

/** Today's AI spend against the daily allowance: chat, search and (for teachers) indexing their PDFs. */
@Component({selector: 'app-usage', standalone: true, template: `
<div class="usage" [attr.aria-busy]="loading()">
  <span class="usage-label" id="usage-label">AI use today</span>
  @if (usage(); as u) {
    <meter min="0" [max]="u.limit_usd" [value]="u.spent_usd" [attr.low]="u.limit_usd * 0.8" [attr.high]="u.limit_usd * 0.95" [attr.optimum]="0" aria-labelledby="usage-label usage-text"></meter>
    <span id="usage-text">{{ spentText(u) }} of {{ limitText(u) }}</span>
    @if (u.spent_usd >= u.limit_usd) {
      <small class="error">Today’s allowance is used up. The assistant and PDF indexing resume at {{ resets(u) }}.</small>
    } @else {
      <small>Resets at {{ resets(u) }}.</small>
    }
  } @else if (loading()) {
    <small>Checking…</small>
  } @else if (failed()) {
    <small class="error">AI use could not be loaded.</small>
  }
  <button type="button" class="secondary usage-refresh" (click)="refresh()" [disabled]="loading()">Refresh AI use</button>
</div>`})
export class UsageComponent implements OnInit {
  readonly api = inject(Api);
  readonly usage = signal<Usage | null>(null);
  readonly loading = signal(false);
  readonly failed = signal(false);

  ngOnInit(): void { this.refresh(); }

  refresh(): void {
    if (this.loading()) return;
    this.loading.set(true);
    this.failed.set(false);
    this.api.usage()
      .then(usage => this.usage.set(usage))
      .catch(() => { this.usage.set(null); this.failed.set(true); })
      .finally(() => this.loading.set(false));
  }

  spentText(u: Usage): string { return dollars(u.spent_usd); }
  limitText(u: Usage): string { return dollars(u.limit_usd); }
  resets(u: Usage): string { return resetTime(u.resets_at); }
}
