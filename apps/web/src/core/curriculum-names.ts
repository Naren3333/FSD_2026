import { Injectable, computed, inject, signal } from '@angular/core';
import { CurriculumApi } from '../api/clients';
import type { Topic } from '../api/models';
import { topicNames } from '../shared/names';
import { Session } from './session';

/* targeted names for evidence; explicit full reads supply teacher pickers. */
@Injectable({ providedIn: 'root' })
export class CurriculumNames {
  private readonly api = inject(CurriculumApi);
  private readonly session = inject(Session);
  private readonly user = computed(() => this.session.profile()?.id ?? '');
  private readonly cache = signal<{ user: string; topics: Topic[]; archived: Topic[] } | null>(null);
  private request = 0;
  private readonly targeted = signal<{ user: string; names: Map<string, string> } | null>(null);
  private pending: Promise<void> = Promise.resolve();

  private readonly mine = computed(() => {
    const cache = this.cache();
    return cache && cache.user === this.user() ? cache : null;
  });
  /* the signed-in user's active topics; empty until read, and never another user's. */
  readonly topics = computed(() => this.mine()?.topics ?? []);
  /* the signed-in user's archived topics, in the same way. */
  readonly archived = computed(() => this.mine()?.archived ?? []);
  private readonly names = computed(() => topicNames([...this.topics(), ...this.archived()]));

  /* the topic's name, or '' when the curriculum has no such topic. */
  topicName(topicId: string): string {
    return (
      this.names().get(topicId) ??
      (this.targeted()?.user === this.user() ? this.targeted()?.names.get(topicId) : '') ??
      ''
    );
  }

  /* reads the topics now, and shares what it read with every page. */
  refresh(): Promise<Topic[]> {
    const user = this.user();
    const request = ++this.request;
    this.targeted.set(null);
    const topics = Promise.all([this.api.topics(), this.api.archivedTopics()]).then(([topics, archived]) => {
      if (request === this.request) this.cache.set({ user, topics, archived });
      return topics;
    });
    return topics;
  }

  /* name only the requested topics; retain at most 512 recent names, including misses. */
  need(topicIds: Iterable<string>): Promise<void> {
    const user = this.user();
    const ids = [...new Set(topicIds)];
    const request = this.request;
    const work = this.pending
      .catch(() => undefined)
      .then(async () => {
        if (this.user() !== user || request !== this.request) return;
        const names = new Map(this.targeted()?.user === user ? this.targeted()?.names : []);
        const missing = ids.filter((id) => !this.names().has(id) && !names.has(id));
        for (let start = 0; start < missing.length; start += 50) {
          const batch = missing.slice(start, start + 50);
          const topics = await this.api.topicNames(batch);
          if (this.user() !== user || request !== this.request) return;
          const found = topicNames(topics);
          for (const id of batch) names.set(id, found.get(id) ?? '');
        }
        for (const id of ids) {
          const value = names.get(id);
          if (value !== undefined) {
            names.delete(id);
            names.set(id, value);
          }
        }
        while (names.size > 512) names.delete(names.keys().next().value!);
        this.targeted.set({ user, names });
      });
    this.pending = work;
    return work;
  }
}
