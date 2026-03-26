import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  testMatch: '*.spec.mjs',
  timeout: 15000,
  use: {
    baseURL: 'http://127.0.0.1:9664',
    headless: true,
  },
});
