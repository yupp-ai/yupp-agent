import { defineConfig, devices } from '@playwright/test'

// Tests run on a separate port from the dev server so we don't fight the
// user's running `bun run dev` and so we can inject COUCH_DEV_BYPASS_AUTH_*
// env vars cleanly.
const PORT = 3011
const BASE_URL = `http://localhost:${PORT}`

export default defineConfig({
  testDir: './tests/e2e',
  testMatch: /.*\.spec\.ts$/,
  testIgnore: ['**/node_modules/**', '../../apps/war-room/**', '../../ypl/**'],
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: BASE_URL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    // Tests run their own dev server. Stop your `bun run dev` first
    // (Next.js 16 refuses to start a second dev server in the same project).
    command: `next dev -p ${PORT}`,
    url: BASE_URL,
    reuseExistingServer: !process.env.CI,
    timeout: 90_000,
    env: {
      // Dev-only bypass — see lib/auth/get-session.ts. Never set in prod.
      COUCH_DEV_BYPASS_AUTH_EMAIL: 'test@example.com',
      COUCH_DEV_BYPASS_AUTH_USER_ID: '00000000-0000-0000-0000-000000000000',
      NODE_ENV: 'development',
    },
  },
})
