import { Injectable, computed, inject } from '@angular/core';
import { ChatApi } from '../api/clients';
import type { Usage } from '../api/models';
import { Section } from '../shared/section';

/* the signed-in user's ai spend today, shared by the meter and the sections that spend it. */
@Injectable({ providedIn: 'root' })
export class AiUsage {
  private readonly chat = inject(ChatApi);
  private readonly section = new Section<Usage>();
  /* kept on show while it is read again; null once a read fails. */
  readonly usage = this.section.value.asReadonly();
  readonly loading = this.section.loading.asReadonly();
  readonly failed = computed(() => !!this.section.error());

  /* reads today's spend again; the newest read wins, so spend from a turn that just ended is never lost. */
  refresh(): void {
    void this.section.load(() => this.chat.usage());
  }
}
