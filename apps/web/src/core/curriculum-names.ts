import { Injectable, computed, inject, signal } from '@angular/core';
import { CurriculumApi } from '../api/clients';
import type { Topic } from '../api/models';
import { topicNames } from '../shared/names';
import { Session } from './session';

/* the organization's topics, active and archived, read once per signed-in user and shared by every page that names topics. */
@Injectable({ providedIn: 'root' })
export class CurriculumNames {
  private readonly api = inject(CurriculumApi);
  private readonly session = inject(Session);
  private readonly user = computed(() => this.session.profile()?.id ?? '');
  private readonly cache = signal<{ user: string; topics: Topic[]; archived: Topic[] } | null>(null);
  private reading: { user: string; topics: Promise<Topic[]> } | null = null;
  private request = 0;

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
    return this.names().get(topicId) ?? '';
  }

  /* reads the topics now, and shares what it read with every page. */
  refresh(): Promise<Topic[]> {
    const user = this.user();
    const request = ++this.request;
    const topics = Promise.all([this.api.topics(), this.api.archivedTopics()]).then(([topics, archived]) => {
      if (request === this.request) this.cache.set({ user, topics, archived });
      return topics;
    });
    this.reading = { user, topics };
    const forget = () => {
      if (this.reading?.topics === topics) this.reading = null;
    };
    topics.then(forget, forget);
    return topics;
  }

  /* resolves once the topics have been read for this user and name each of these topics, reading them again (or joining a read already under way) when they do not. */
  async need(topicIds: Iterable<string>): Promise<void> {
    const user = this.user();
    const known = this.cache()?.user === user;
    if (known && [...topicIds].every((id) => this.names().has(id))) return;
    await (this.reading?.user === user ? this.reading.topics : this.refresh());
  }
}
