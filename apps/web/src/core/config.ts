import { InjectionToken } from '@angular/core';

/** Where the web app finds the gateway and the identity provider. */
export interface AppConfig {
  /** Kong's base URL; each backend is under `/api/<service>`. */
  gatewayUrl: string;
  keycloak: { url: string; realm: string; clientId: string };
  /** How long an ordinary API call may take, in milliseconds. */
  requestTimeout: number;
}

/** The local Docker Compose stack (docker-compose.yml). */
export const LOCAL_CONFIG: AppConfig = {
  gatewayUrl: 'http://localhost:8000',
  keycloak: { url: 'http://localhost:8081', realm: 'learning', clientId: 'learning-web' },
  requestTimeout: 20_000,
};

export const APP_CONFIG = new InjectionToken<AppConfig>('APP_CONFIG', {
  providedIn: 'root',
  factory: () => LOCAL_CONFIG,
});
