import { Injectable, inject } from '@angular/core';
import Keycloak from 'keycloak-js';
import { APP_CONFIG } from './config';

/** Sign-in through Keycloak (authorization code with PKCE) and the bearer token for API calls. */
@Injectable({ providedIn: 'root' })
export class Auth {
  private readonly keycloak = new Keycloak(inject(APP_CONFIG).keycloak);

  /**
   * Completes a sign-in redirect if there is one, otherwise asks Keycloak (without showing its login page) whether
   * this browser is still signed in, so a page refresh keeps the user signed in. Tokens stay in memory only.
   * Run before the router reads the URL.
   */
  async initialize(): Promise<void> {
    await this.keycloak.init({
      onLoad: 'check-sso',
      checkLoginIframe: false,
      pkceMethod: 'S256',
      responseMode: 'query',
    });
  }

  get authenticated(): boolean {
    return !!this.keycloak.authenticated;
  }

  login(): Promise<void> {
    return this.keycloak.login({ redirectUri: location.origin + '/' });
  }

  logout(): Promise<void> {
    return this.keycloak.logout({ redirectUri: location.origin + '/' });
  }

  /** A token valid for at least 30 more seconds, refreshed if needed. */
  async token(): Promise<string> {
    if (!this.authenticated) throw new Error('Sign in to continue.');
    try {
      await this.keycloak.updateToken(30);
    } catch {
      throw new Error('Your session expired. Sign in again; your unsaved work is still here.');
    }
    return this.keycloak.token ?? '';
  }
}
