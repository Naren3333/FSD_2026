import { Injectable, inject, signal } from '@angular/core';
import { ChatApi } from '../api/clients';
import type { Usage } from '../api/models';

/** The signed-in user's AI spend today, shared by the meter and the sections that spend it. */
@Injectable({ providedIn: 'root' })
export class AiUsage {
  private readonly chat = inject(ChatApi);
  readonly usage = signal<Usage | null>(null);
  readonly loading = signal(false);
  readonly failed = signal(false);

  refresh(): void {
    if (this.loading()) return;
    this.loading.set(true);
    this.failed.set(false);
    this.chat
      .usage()
      .then((usage) => this.usage.set(usage))
      .catch(() => {
        this.usage.set(null);
        this.failed.set(true);
      })
      .finally(() => this.loading.set(false));
  }
}
