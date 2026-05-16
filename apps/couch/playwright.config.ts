import { defineConfig, devices } from '@playwright/test'

const PORT = 3010
const BASE_URL = `http://localhost:${PORT}`

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'npm run dev',
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: {
      // Smoke tests don't need AHS. Set a fake AHS URL so server-side
      // calls fail fast and the unauth path is exercised.
      AHS_BASE_URL: 'http://127.0.0.1:9',
      AHS_API_KEY: 'unused',
      AUTH_SECRET: 'test'.repeat(16),
    },
  },
})
