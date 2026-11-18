import { Injectable, inject, signal } from '@angular/core';
import Keycloak from 'keycloak-js';
import { operatorPolicy } from '../api/generated/monitoring';
import { APP_CONFIG } from './config';

/* sign-in through keycloak (authorization code with pkce) and the bearer token for api calls. */
@Injectable({ providedIn: 'root' })
export class Auth {
  private readonly keycloak = new Keycloak(inject(APP_CONFIG).keycloak);
  /* signed in once, but the session can no longer be refreshed (or an api refused the token): the user must sign in again. */
  readonly expired = signal(false);
  /* the token's operator role, kept in step with each sign-in, refresh and sign-out. */
  private readonly operatorRole = signal(false);

  /* completes a sign-in redirect if there is one, otherwise asks keycloak (without showing its login page) whether this browser is still signed in, so a page refresh keeps the user signed in. */
  async initialize(): Promise<void> {
    const sync = () => this.operatorRole.set(this.hasOperatorRole());
    this.keycloak.onAuthSuccess = sync;
    this.keycloak.onAuthRefreshSuccess = sync;
    this.keycloak.onAuthLogout = sync;
    await this.keycloak.init({
      onLoad: 'check-sso',
      checkLoginIframe: false,
      pkceMethod: 'S256',
      responseMode: 'query',
    });
    sync();
  }

  get authenticated(): boolean {
    return !!this.keycloak.authenticated;
  }

  get operator(): boolean {
    return this.operatorRole();
  }

  private hasOperatorRole(): boolean {
    return (
      this.authenticated &&
      this.keycloak.hasRealmRole(operatorPolicy.required_role) &&
      !operatorPolicy.excluded_roles.some((role) => this.keycloak.hasRealmRole(role))
    );
  }

  login(): Promise<void> {
    return this.keycloak.login({ redirectUri: location.origin + '/' });
  }

  logout(): Promise<void> {
    return this.keycloak.logout({ redirectUri: location.origin + '/' });
  }

  /* a token valid for at least 30 more seconds, refreshed if needed. */
  async token(): Promise<string> {
    if (!this.authenticated) throw new Error('Sign in to continue.');
    try {
      await this.keycloak.updateToken(30);
    } catch {
      this.expired.set(true);
      throw new Error('Your session expired. Sign in again; your unsaved work is still here.');
    }
    return this.keycloak.token ?? '';
  }
}
