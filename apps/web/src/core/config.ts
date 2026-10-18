import { InjectionToken } from '@angular/core';

/** Where the web app finds the gateway and the identity provider. */
export interface AppConfig {
  /** Kong's base URL; each backend is under `/api/<service>`. */
  gatewayUrl: string;
  keycloak: { url: string; realm: string; clientId: string };
  /** How long an ordinary API call may take, in milliseconds. */
  requestTimeout: number;
}

/** The local Docker Compose development stack (docker-compose.yml) on its default ports. */
export const LOCAL_CONFIG: AppConfig = {
  gatewayUrl: 'http://localhost:8000',
  keycloak: { url: 'http://localhost:8081', realm: 'learning', clientId: 'learning-web' },
  requestTimeout: 20_000,
};

export const APP_CONFIG = new InjectionToken<AppConfig>('APP_CONFIG', {
  providedIn: 'root',
  factory: () => LOCAL_CONFIG,
});

/** The gateway and Keycloak of the stack serving the app, from its web server's /config.json (nginx.conf), so one
 * build works on whichever ports a Compose stack publishes. LOCAL_CONFIG when there is none, such as under
 * `ng serve`, whose fallback answers with index.html. */
export async function loadConfig(fetcher: typeof fetch = fetch): Promise<AppConfig> {
  try {
    const response = await fetcher('/config.json', { cache: 'no-store' });
    const body: unknown = response.ok ? await response.json() : null;
    if (!body || typeof body !== 'object') return LOCAL_CONFIG;
    const { gatewayUrl, keycloakUrl } = body as Record<string, unknown>;
    if (typeof gatewayUrl !== 'string' || typeof keycloakUrl !== 'string') return LOCAL_CONFIG;
    return { ...LOCAL_CONFIG, gatewayUrl, keycloak: { ...LOCAL_CONFIG.keycloak, url: keycloakUrl } };
  } catch {
    return LOCAL_CONFIG;
  }
}
