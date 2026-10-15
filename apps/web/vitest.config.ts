import { defineConfig } from 'vitest/config';
// Worker threads with a small cap: Vitest gives each worker 5 s to start, and on a slow Windows disk
// several workers starting at once can miss that, failing the run with "Timeout starting threads runner".
export default defineConfig({
  test: { include: ['src/**/*.test.ts'], setupFiles: ['vitest.setup.ts'], pool: 'threads', maxWorkers: 2 },
});
