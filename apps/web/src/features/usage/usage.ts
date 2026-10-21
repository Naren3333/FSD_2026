import { Component, OnInit, inject } from '@angular/core';
import type { Usage } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { dollars, resetTime } from '../../shared/format';

/* today's ai spend against the daily allowance: chat, search and (for teachers) indexing their pdfs. */
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
