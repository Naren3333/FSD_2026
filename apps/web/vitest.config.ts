import { defineConfig } from 'vitest/config';
// limit worker startup concurrency on slow disks.
export default defineConfig({
  test: { include: ['src/**/*.test.ts'], setupFiles: ['vitest.setup.ts'], pool: 'threads', maxWorkers: 2 },
});
