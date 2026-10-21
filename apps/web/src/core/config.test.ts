import { describe, expect, it } from 'vitest';
import { LOCAL_CONFIG, loadConfig } from './config';

/* a fetch that answers every request with one response, or fails like an unreachable server. */
function answering(response: Response | Error): typeof fetch {
  return () => (response instanceof Error ? Promise.reject(response) : Promise.resolve(response));
}

describe('loadConfig', () => {
  it("uses the stack's gateway and Keycloak from /config.json", async () => {
    const body = { gatewayUrl: 'http://localhost:18000', keycloakUrl: 'http://localhost:18081' };
    const config = await loadConfig(answering(Response.json(body)));
    expect(config.gatewayUrl).toBe('http://localhost:18000');
    expect(config.keycloak).toEqual({ ...LOCAL_CONFIG.keycloak, url: 'http://localhost:18081' });
    expect(config.requestTimeout).toBe(LOCAL_CONFIG.requestTimeout);
  });

  it.each([
    ['the dev server answers with index.html', new Response('<!doctype html>', { status: 200 })],
    ['there is no /config.json', new Response('', { status: 404 })],
    ['a value is missing', Response.json({ gatewayUrl: 'http://localhost:18000' })],
    ['a value is not text', Response.json({ gatewayUrl: 18000, keycloakUrl: 'http://localhost:18081' })],
    ['the server is unreachable', new TypeError('Failed to fetch')],
  ])('falls back to the local stack when %s', async (_case, response) => {
    expect(await loadConfig(answering(response))).toBe(LOCAL_CONFIG);
  });
});
