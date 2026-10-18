import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { IdentityApi } from '../api/clients';
import type { Classroom } from '../api/models';
import { messageOf } from '../shared/errors';

/** The user's classrooms and the one every section is working in. */
@Injectable({ providedIn: 'root' })
export class ClassroomContext {
  private readonly identity = inject(IdentityApi);
  readonly classrooms = signal<Classroom[]>([]);
  readonly classroomId = signal('');
  /** Whether a list has loaded yet: until then `classrooms` is empty only because it is unknown. */
  readonly known = signal(false);
  readonly loading = signal(false);
  /** Why the last reload failed, or '' after one succeeds. */
  readonly error = signal('');
  /** The current classroom's name, or '' when none is chosen. */
  readonly classroomName = computed(
    () => this.classrooms().find((room) => room.id === this.classroomId())?.name ?? '',
  );

  private request = 0;
  private latest: Promise<void> = Promise.resolve();

  /** Reloads the list, keeping the current classroom when it is still there. Only the newest reload lands: an older
   * one that answers later (say, from before a classroom was deleted) waits for the newest and reports its outcome. */
  refresh(): Promise<void> {
    const read = this.read(++this.request);
    this.latest = read;
    return read;
  }

  private async read(request: number): Promise<void> {
    this.loading.set(true);
    try {
      const rooms = await this.identity.classrooms();
      if (request !== this.request) return this.latest;
      this.classrooms.set(rooms);
      this.known.set(true);
      this.error.set('');
      if (!rooms.some((room) => room.id === this.classroomId())) this.classroomId.set(rooms[0]?.id || '');
    } catch (error) {
      if (request !== this.request) return this.latest;
      this.error.set(messageOf(error, 'Your classrooms could not be loaded.'));
      throw error;
    } finally {
      if (request === this.request) this.loading.set(false);
    }
  }

  select(classroomId: string): void {
    this.classroomId.set(classroomId);
  }
}

/** Runs `reload` now and whenever the current classroom changes. Call from a component's constructor. */
export function onClassroomChange(reload: (classroomId: string) => void): void {
  const classroomId = inject(ClassroomContext).classroomId;
  effect(() => {
    const id = classroomId();
    untracked(() => reload(id));
  });
}
