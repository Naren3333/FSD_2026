import { defineConfig } from '@playwright/test';

// run browser tests against the isolated test stack to protect development data.
const TEST_PORTS: Record<string, string> = { KONG_PORT: '18000', KEYCLOAK_PORT: '18081', WEB_PORT: '14200' };
if (process.env['E2E_DEV_STACK'] !== '1') {
  if (process.env['COMPOSE_PROJECT_NAME'] !== 'learning-test') {
    throw new Error(
      'The browser tests run against the test stack: python scripts/stack_tests.py e2e (or set E2E_DEV_STACK=1 to use the development stack).',
    );
  }
  const wrong = Object.keys(TEST_PORTS).filter((name) => process.env[name] !== TEST_PORTS[name]);
  if (wrong.length > 0) {
    throw new Error(
      `COMPOSE_PROJECT_NAME is learning-test, but ${wrong.join(', ')} is not the test stack's port (${Object.entries(
        TEST_PORTS,
      )
        .map(([name, port]) => `${name}=${port}`)
        .join(', ')}). Run them through python scripts/stack_tests.py e2e.`,
    );
  }
}

export default defineConfig({
  testDir: 'e2e',
  timeout: 120000,
  expect: { timeout: 20000 },
  use: {
    baseURL: `http://localhost:${process.env['WEB_PORT'] || '4200'}`,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  workers: 1,
});
