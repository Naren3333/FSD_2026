import { Injectable, computed, inject, signal } from '@angular/core';
import { CurriculumApi } from '../api/clients';
import type { Node } from '../api/models';
import { skillNames } from '../shared/names';
import { Session } from './session';

/** The organization's curriculum, read once per signed-in user and shared by every page that names skills. It is
 * read again when a page needs a skill it does not know yet (another teacher may have added it) and whenever the
 * curriculum page refreshes or adds to it. */
@Injectable({ providedIn: 'root' })
export class CurriculumNames {
  private readonly api = inject(CurriculumApi);
  private readonly session = inject(Session);
  private readonly user = computed(() => this.session.profile()?.id ?? '');
  private readonly cache = signal<{ user: string; nodes: Node[] } | null>(null);
  private reading: { user: string; nodes: Promise<Node[]> } | null = null;
  private request = 0;

  /** The signed-in user's curriculum; empty until read, and never another user's. */
  readonly nodes = computed(() => {
    const cache = this.cache();
    return cache && cache.user === this.user() ? cache.nodes : [];
  });
  readonly skills = computed(() => this.nodes().filter((node) => node.kind === 'skill'));
  private readonly names = computed(() => skillNames(this.nodes()));

  /** The skill's name, or '' when the curriculum has no such skill. */
  skillName(skillId: string): string {
    return this.names().get(skillId) ?? '';
  }

  /** Reads the curriculum now, and shares what it read with every page. */
  refresh(): Promise<Node[]> {
    const user = this.user();
    const request = ++this.request;
    const nodes = this.api.nodes().then((nodes) => {
      if (request === this.request) this.cache.set({ user, nodes });
      return nodes;
    });
    this.reading = { user, nodes };
    const forget = () => {
      if (this.reading?.nodes === nodes) this.reading = null;
    };
    nodes.then(forget, forget);
    return nodes;
  }

  /** Resolves once the curriculum has been read for this user and names each of these skills, reading it again
   * (or joining a read already under way) when it does not. */
  async need(skillIds: Iterable<string>): Promise<void> {
    const user = this.user();
    const known = this.cache()?.user === user;
    if (known && [...skillIds].every((id) => this.names().has(id))) return;
    await (this.reading?.user === user ? this.reading.nodes : this.refresh());
  }
}
