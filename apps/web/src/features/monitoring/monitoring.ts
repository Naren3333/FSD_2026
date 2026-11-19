import { DatePipe, DecimalPipe } from '@angular/common';
import { Component, DestroyRef, DOCUMENT, HostListener, OnInit, effect, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule } from '@angular/forms';
import type { components } from '../../api/generated/monitoring';
import { ApiError, Gateway } from '../../core/gateway';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { messageOf } from '../../shared/errors';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { StatusComponent } from '../../shared/status';

type MonitoringStatus = components['schemas']['MonitoringStatus'];
type SpendSummary = components['schemas']['SpendSummary'];
type UserSpendPage = components['schemas']['UserSpendPage'];
type Period = 'today' | 'last_7_days';

const NUMBERS = new Intl.NumberFormat('en-SG');
const TIMES = new Map<string, Intl.DateTimeFormat>();

/* one reading-time format per timezone; the browser's when none is given. */
function timeFormat(timeZone = ''): Intl.DateTimeFormat {
  let format = TIMES.get(timeZone);
  if (!format) {
    format = new Intl.DateTimeFormat('en-SG', {
      timeZone: timeZone || undefined,
      day: 'numeric',
      month: 'short',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
      timeZoneName: 'short',
    });
    TIMES.set(timeZone, format);
  }
  return format;
}

class MonitoringSection<T> {
  readonly feedback = new ActionState();
  readonly data = signal<T | null>(null);
  readonly pending = signal(false);
  request: AbortController | null = null;
}

@Component({
  standalone: true,
  templateUrl: './monitoring.html',
  styleUrl: './monitoring.css',
  imports: [
    DatePipe,
    DecimalPipe,
    PagerComponent,
    ReactiveFormsModule,
    RefreshButtonComponent,
    StatusComponent,
  ],
})
export class MonitoringComponent implements OnInit {
  private readonly gateway = inject(Gateway);
  private readonly session = inject(Session);
  private readonly destroy = inject(DestroyRef);
  private readonly document = inject(DOCUMENT);
  private disposed = false;
  readonly blocked = signal('');
  readonly status = new MonitoringSection<MonitoringStatus>();
  readonly spend = new MonitoringSection<SpendSummary>();
  readonly users = new MonitoringSection<UserSpendPage>();
  readonly pageSize = 20;
  page = 0;
  readonly filters = inject(FormBuilder).nonNullable.group({
    user_id: '',
    period: 'today' as Period,
  });
  private appliedFilters = this.filters.getRawValue();

  readonly serviceLabels = {
    checking: 'Collecting data',
    ready: 'Ready',
    degraded: 'Degraded',
    not_ready: 'Not ready',
    unreachable: 'Unreachable',
    discovery_failed: 'Discovery failed',
  } as const;

  readonly topicLabels = {
    checking: 'Collecting data',
    available: 'Available',
    stale: 'Stale',
    unavailable: 'Unavailable',
  } as const;

  constructor() {
    effect(() => {
      if (this.session.expired()) this.revoke('Your session expired. Sign in again to view monitoring.');
    });
  }

  ngOnInit(): void {
    this.refreshStatus();
    this.refreshSpend();
    this.loadUsers();
    const statusTimer = setInterval(() => this.refreshStatus(false), 15_000);
    const spendTimer = setInterval(() => this.refreshSpend(false), 60_000);
    this.destroy.onDestroy(() => {
      this.disposed = true;
      clearInterval(statusTimer);
      clearInterval(spendTimer);
      this.clear();
    });
  }

  @HostListener('document:visibilitychange')
  onVisibility(): void {
    if (!this.document.hidden) {
      this.refreshStatus(false);
      this.refreshSpend(false);
    }
  }

  refreshStatus(manual = true): void {
    if (manual ? !this.status.feedback.busy() : !this.status.pending()) {
      void this.read(this.status, '/status', manual);
    }
  }

  refreshSpend(manual = true): void {
    if (manual ? !this.spend.feedback.busy() : !this.spend.pending()) {
      void this.read(this.spend, '/ai-spend', manual);
    }
  }

  /* the filters narrow the admin's own school, the only one monitoring reports (adr 0019). */
  applyFilters(): void {
    const { user_id } = this.filters.getRawValue();
    // a pasted identifier often carries surrounding whitespace that would match no account.
    this.filters.patchValue({ user_id: user_id.trim() });
    this.appliedFilters = this.filters.getRawValue();
    this.changePage(0);
  }

  changePage(page: number): void {
    this.page = page;
    this.users.data.set(null);
    this.loadUsers();
  }

  loadUsers(): void {
    const { period, user_id } = this.appliedFilters;
    const query = new URLSearchParams({ period, offset: String(this.page * this.pageSize) });
    if (user_id) query.set('user_id', user_id);
    void this.read(this.users, `/ai-spend/users?${query}`, true).then((value) => {
      if (value && this.users.data() === value && this.page > 0 && value.items.length === 0) {
        const lastPage = Math.max(0, Math.ceil(value.total / this.pageSize) - 1);
        if (lastPage < this.page) this.changePage(lastPage);
      }
    });
  }

  latencyText(latency: MonitoringStatus['services'][number]['latency']): string {
    if (latency.state === 'checking') return 'Collecting data';
    if (latency.state === 'unavailable') return 'Unavailable';
    if (!latency.requests)
      return latency.state === 'partial' ? 'No traffic in sampled replicas' : 'No traffic';
    if (latency.above_max_bucket) return 'Over 300,000 ms';
    return `≤ ${NUMBERS.format(latency.p95_ms ?? 0)} ms`;
  }

  /* a reading time, naming its timezone: the budget's for expenditure, the browser's otherwise. */
  when(value: string | null | undefined, timeZone?: string): string {
    if (!value) return '';
    return timeFormat(timeZone).format(new Date(value));
  }

  usd(value: string): string {
    const [whole, fraction = ''] = value.split('.');
    const dollars = NUMBERS.format(BigInt(whole));
    return `USD ${dollars}.${fraction.replace(/0+$/, '').padEnd(2, '0')}`;
  }

  private async read<T>(
    section: MonitoringSection<T>,
    path: string,
    manual: boolean,
  ): Promise<T | undefined> {
    if (this.disposed || this.blocked() || (!manual && this.document.hidden)) return;
    section.request?.abort();
    const request = new AbortController();
    section.request = request;
    section.pending.set(true);
    section.feedback.busy.set(manual);
    if (manual) section.feedback.error.set('');
    try {
      const data = await this.gateway.request<T>('monitoring', path, {
        timeout: 9_000,
        signal: request.signal,
      });
      if (section.request === request) {
        section.data.set(data);
        section.feedback.error.set('');
        return data;
      }
    } catch (error) {
      if (section.request !== request) return;
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        this.revoke(
          error.status === 401
            ? 'Sign in again to view monitoring.'
            : 'Your account cannot access monitoring.',
        );
      } else {
        const message = messageOf(error, 'The data could not be loaded.');
        section.feedback.error.set(section.data() ? `Showing stale data. ${message}` : message);
      }
    } finally {
      if (section.request === request) {
        section.request = null;
        section.pending.set(false);
        section.feedback.busy.set(false);
      }
    }
  }

  private revoke(message: string): void {
    this.blocked.set(message);
    this.clear();
  }

  private clear(): void {
    for (const section of [this.status, this.spend, this.users]) {
      section.request?.abort();
      section.request = null;
      section.pending.set(false);
      section.feedback.busy.set(false);
      section.data.set(null);
    }
    this.filters.reset();
    this.appliedFilters = this.filters.getRawValue();
  }
}
