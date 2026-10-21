import { Injectable, computed, inject, signal } from '@angular/core';
import { IdentityApi } from '../api/clients';
import type { Profile } from '../api/models';
import { Auth } from './auth';

/* the signed-in user: their profile and role, once identity has registered them. */
@Injectable({ providedIn: 'root' })
export class Session {
  private readonly auth = inject(Auth);
  private readonly identity = inject(IdentityApi);
  readonly profile = signal<Profile | null>(null);
  readonly isTeacher = computed(() => this.profile()?.role === 'teacher');
  readonly isStudent = computed(() => this.profile()?.role === 'student');

  /* the sign-in has expired; signing in again is the way back. */
  readonly expired = this.auth.expired;

  get signedIn(): boolean {
    return this.auth.authenticated;
  }

  private loading: Promise<void> | null = null;

  /* registers the signed-in user with identity and loads their profile, once; a failed load can be retried. */
  ready(): Promise<void> {
    this.loading ??= this.load().catch((error: unknown) => {
      this.loading = null;
      throw error;
    });
    return this.loading;
  }

  private async load(): Promise<void> {
    if (this.auth.authenticated) this.profile.set(await this.identity.register());
  }

  login(): Promise<void> {
    return this.auth.login();
  }

  logout(): Promise<void> {
    return this.auth.logout();
  }
}
