import { defineConfig } from 'vitest/config';
// Worker threads, not child processes: on Windows the process pool can time out starting the jsdom test file.
export default defineConfig({ test: { include: ['src/**/*.test.ts'], pool: 'threads' } });
