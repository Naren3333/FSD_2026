import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { IdentityApi } from '../api/clients';
import type { Classroom } from '../api/models';
import { messageOf } from '../shared/errors';

/* the user's classrooms and the one every section is working in. */
@Injectable({ providedIn: 'root' })
export class ClassroomContext {
  private readonly identity = inject(IdentityApi);
  readonly classrooms = signal<Classroom[]>([]);
  readonly classroomId = signal('');
  /* whether a list has loaded yet: until then `classrooms` is empty only because it is unknown. */
  readonly known = signal(false);
  readonly loading = signal(false);
  /* why the last reload failed, or '' after one succeeds. */
  readonly error = signal('');
  /* the current classroom's name, or '' when none is chosen. */
  readonly classroomName = computed(
    () => this.classrooms().find((room) => room.id === this.classroomId())?.name ?? '',
  );

  private request = 0;
  private latest: Promise<void> = Promise.resolve();
  /* the signed-in user, whose last chosen classroom is remembered in this browser. */
  private userId = '';

  /* names the user whose choice is saved and restored; call before the first refresh. */
  setUser(id: string): void {
    this.userId = id;
  }

  /* reloads the list, keeping the current classroom when it is still there. */
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
      const has = (id: string) => !!id && rooms.some((room) => room.id === id);
      const saved = this.saved();
      if (saved && !has(saved)) this.save('');
      if (!has(this.classroomId())) this.classroomId.set(has(saved) ? saved : rooms[0]?.id || '');
    } catch (error) {
      if (request !== this.request) return this.latest;
      this.error.set(messageOf(error, 'Your classrooms could not be loaded.'));
      throw error;
    } finally {
      if (request === this.request) this.loading.set(false);
    }
  }

  /* an explicit choice, remembered for the next visit; '' forgets it. */
  select(classroomId: string): void {
    this.classroomId.set(classroomId);
    this.save(classroomId);
  }

  private key(): string {
    return `ledger.classroom.${this.userId}`;
  }

  /* storage can be missing or blocked; the app works without it. */
  private saved(): string {
    if (!this.userId) return '';
    try {
      return localStorage.getItem(this.key()) ?? '';
    } catch {
      return '';
    }
  }

  private save(classroomId: string): void {
    if (!this.userId) return;
    try {
      if (classroomId) localStorage.setItem(this.key(), classroomId);
      else localStorage.removeItem(this.key());
    } catch {
      // the choice just isn't remembered.
    }
  }
}

/* runs `reload` now and whenever the current classroom changes. */
export function onClassroomChange(reload: (classroomId: string) => void): void {
  const classroomId = inject(ClassroomContext).classroomId;
  effect(() => {
    const id = classroomId();
    untracked(() => reload(id));
  });
}
