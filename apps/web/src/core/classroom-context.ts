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

  /** Reloads the list, keeping the current classroom when it is still there. */
  async refresh(): Promise<void> {
    this.loading.set(true);
    try {
      const rooms = await this.identity.classrooms();
      this.classrooms.set(rooms);
      this.known.set(true);
      this.error.set('');
      if (!rooms.some((room) => room.id === this.classroomId())) this.classroomId.set(rooms[0]?.id || '');
    } catch (error) {
      this.error.set(messageOf(error, 'Your classrooms could not be loaded.'));
      throw error;
    } finally {
      this.loading.set(false);
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
