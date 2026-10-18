import { Injectable, computed, inject, signal } from '@angular/core';
import { CurriculumApi } from '../api/clients';
import type { Topic } from '../api/models';
import { topicNames } from '../shared/names';
import { Session } from './session';

/** The organization's topics, read once per signed-in user and shared by every page that names topics. They are
 * read again when a page needs a topic it does not know yet (another teacher may have added it) and whenever the
 * curriculum page refreshes or adds to them. */
@Injectable({ providedIn: 'root' })
export class CurriculumNames {
  private readonly api = inject(CurriculumApi);
  private readonly session = inject(Session);
  private readonly user = computed(() => this.session.profile()?.id ?? '');
  private readonly cache = signal<{ user: string; topics: Topic[] } | null>(null);
  private reading: { user: string; topics: Promise<Topic[]> } | null = null;
  private request = 0;

  /** The signed-in user's topics; empty until read, and never another user's. */
  readonly topics = computed(() => {
    const cache = this.cache();
    return cache && cache.user === this.user() ? cache.topics : [];
  });
  private readonly names = computed(() => topicNames(this.topics()));

  /** The topic's name, or '' when the curriculum has no such topic. */
  topicName(topicId: string): string {
    return this.names().get(topicId) ?? '';
  }

  /** Reads the topics now, and shares what it read with every page. */
  refresh(): Promise<Topic[]> {
    const user = this.user();
    const request = ++this.request;
    const topics = this.api.topics().then((topics) => {
      if (request === this.request) this.cache.set({ user, topics });
      return topics;
    });
    this.reading = { user, topics };
    const forget = () => {
      if (this.reading?.topics === topics) this.reading = null;
    };
    topics.then(forget, forget);
    return topics;
  }

  /** Resolves once the topics have been read for this user and name each of these topics, reading them again
   * (or joining a read already under way) when they do not. */
  async need(topicIds: Iterable<string>): Promise<void> {
    const user = this.user();
    const known = this.cache()?.user === user;
    if (known && [...topicIds].every((id) => this.names().has(id))) return;
    await (this.reading?.user === user ? this.reading.topics : this.refresh());
  }
}
