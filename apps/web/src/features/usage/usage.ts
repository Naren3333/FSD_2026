import { Component, OnInit, inject } from '@angular/core';
import type { Usage } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';

/** Dollars as the meter shows them: cents, and never "$0.00" for spend that is real but under a cent. */
export function dollars(value: number): string {
  if (value > 0 && value < 0.005) return 'under $0.01';
  return new Intl.NumberFormat('en-SG', {
    style: 'currency',
    currency: 'USD',
    currencyDisplay: 'narrowSymbol',
  }).format(value);
}

/** When the allowance comes back, in the viewer's local time: today's or tomorrow's date only when it matters. */
export function resetTime(iso: string, now = new Date()): string {
  const at = new Date(iso);
  const time = at.toLocaleTimeString('en-SG', { hour: 'numeric', minute: '2-digit' });
  return at.toDateString() === now.toDateString()
    ? time
    : `${time} on ${at.toLocaleDateString('en-SG', { day: 'numeric', month: 'short' })}`;
}

/** Today's AI spend against the daily allowance: chat, search and (for teachers) indexing their PDFs. */
@Component({ selector: 'app-usage', standalone: true, templateUrl: './usage.html' })
export class UsageComponent implements OnInit {
  private readonly meter = inject(AiUsage);
  readonly usage = this.meter.usage;
  readonly loading = this.meter.loading;
  readonly failed = this.meter.failed;

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.meter.refresh();
  }

  spentText(u: Usage): string {
    return dollars(u.spent_usd);
  }
  limitText(u: Usage): string {
    return dollars(u.limit_usd);
  }
  resets(u: Usage): string {
    return resetTime(u.resets_at);
  }
}
