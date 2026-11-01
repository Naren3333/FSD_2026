import { defineConfig } from '@playwright/test';

// run browser tests against the isolated test stack to protect development data.
const TEST_STACK: Record<string, string> = {
  KONG_PORT: '18000',
  KEYCLOAK_PORT: '18081',
  EDGE_PORT: '18443',
  APP_URL: 'https://app.localhost:18443',
};
if (process.env['E2E_DEV_STACK'] !== '1') {
  if (process.env['COMPOSE_PROJECT_NAME'] !== 'learning-test') {
    throw new Error(
      'The browser tests run against the test stack: python scripts/stack_tests.py e2e (or set E2E_DEV_STACK=1 to use the development stack).',
    );
  }
  const wrong = Object.keys(TEST_STACK).filter((name) => process.env[name] !== TEST_STACK[name]);
  if (wrong.length > 0) {
    throw new Error(
      `COMPOSE_PROJECT_NAME is learning-test, but ${wrong.join(', ')} is not the test stack's (${Object.entries(
        TEST_STACK,
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
    // the app behind the stack's edge, whose certificate comes from the edge's own local authority.
    baseURL: process.env['APP_URL'] || 'https://app.localhost',
    ignoreHTTPSErrors: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  workers: 1,
});
