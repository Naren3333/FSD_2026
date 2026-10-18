import { defineConfig } from '@playwright/test';

// The browser tests create classrooms, documents and assessments, so they run against the disposable test stack:
// `python scripts/stack_tests.py e2e` starts it, runs them with COMPOSE_PROJECT_NAME naming it and its ports in
// KONG_PORT, KEYCLOAK_PORT and WEB_PORT, and removes it. E2E_DEV_STACK=1 runs them against the development stack.
if (process.env['COMPOSE_PROJECT_NAME'] !== 'learning-test' && process.env['E2E_DEV_STACK'] !== '1') {
  throw new Error(
    'The browser tests run against the test stack: python scripts/stack_tests.py e2e (or set E2E_DEV_STACK=1 to use the development stack).',
  );
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
