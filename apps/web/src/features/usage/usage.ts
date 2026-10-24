import {
  Component,
  ElementRef,
  HostListener,
  OnInit,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import type { Usage } from '../../api/models';
import { AiUsage } from '../../core/ai-usage';
import { dollars, resetTime } from '../../shared/format';

/* today's ai spend against the daily allowance, as a pill that opens the details: chat, search and (for teachers) indexing their documents. */
@Component({ selector: 'app-usage', standalone: true, templateUrl: './usage.html' })
export class UsageComponent implements OnInit {
  private readonly meter = inject(AiUsage);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);
  @ViewChild('pill') private pill?: ElementRef<HTMLButtonElement>;
  readonly usage = this.meter.usage;
  readonly loading = this.meter.loading;
  readonly failed = this.meter.failed;
  readonly open = signal(false);
  readonly usedUp = computed(() => {
    const u = this.usage();
    return !!u && u.spent_usd >= u.limit_usd;
  });

  ngOnInit(): void {
    this.refresh();
  }

  refresh(): void {
    this.meter.refresh();
  }

  toggle(): void {
    this.open.update((open) => !open);
  }

  @HostListener('document:keydown.escape')
  escape(): void {
    if (!this.open()) return;
    this.open.set(false);
    this.pill?.nativeElement.focus();
  }

  @HostListener('document:click', ['$event'])
  clickOutside(event: MouseEvent): void {
    if (this.open() && !this.host.nativeElement.contains(event.target as Node)) this.open.set(false);
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
