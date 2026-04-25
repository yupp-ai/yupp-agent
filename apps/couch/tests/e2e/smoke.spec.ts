import { expect, test } from '@playwright/test'

/**
 * Couch smoke test — exercises the basic UI surfaces with the dev-only auth
 * bypass on. Requires the AHS tunnel running (~/scripts/ahs-tunnel.sh) so
 * the listAgents / listSessions calls return real data.
 *
 * The bypass is configured in playwright.config.ts via webServer env.
 */

test.describe('landing page', () => {
  test('renders the rail and the prompt', async ({ page }) => {
    await page.goto('/')

    // Brand + nav
    await expect(page.getByRole('link', { name: 'home' })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Sessions' })).toBeVisible()

    // Disabled placeholders
    await expect(page.getByText('Projects', { exact: true })).toBeVisible()
    await expect(page.getByText('Schedules', { exact: true })).toBeVisible()

    // Prompt textarea
    await expect(
      page.getByPlaceholder('Ask Couch anything')
    ).toBeVisible()
  })

  test('agent dropdown opens and contains items', async ({ page }) => {
    await page.goto('/')
    // Wait for agents to load and the trigger to render. With eng-raccoon
    // as default, the trigger should show the display name; otherwise it
    // should at minimum say 'Pick an agent'.
    const trigger = page.locator('[role="combobox"]').first()
    await expect(trigger).toBeVisible({ timeout: 15_000 })

    await trigger.click()
    // Popup options should render with at least one item.
    const items = page.locator('[role="option"]')
    await expect(items.first()).toBeVisible({ timeout: 10_000 })
    const count = await items.count()
    expect(count).toBeGreaterThan(0)
  })

  test('Settings dialog opens and shows Sign out', async ({ page }) => {
    await page.goto('/')
    await page.getByRole('button', { name: 'Settings' }).click()
    await expect(page.getByText('More settings coming soon.')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
    await page.getByRole('button', { name: 'Close' }).click()
    await expect(page.getByText('More settings coming soon.')).not.toBeVisible()
  })

  test('rail toggle collapses and expands', async ({ page }) => {
    await page.goto('/')
    const aside = page.locator('aside').first()

    // Initial expanded width is w-64 (256 px); collapsed is w-16 (64 px).
    const expandedBox = await aside.boundingBox()
    expect(expandedBox?.width).toBeGreaterThan(200)

    await page.getByRole('button', { name: 'toggle rail' }).click()
    // Allow Tailwind transition to settle.
    await page.waitForTimeout(400)
    const collapsedBox = await aside.boundingBox()
    expect(collapsedBox?.width).toBeLessThan(80)
    expect(collapsedBox?.width).toBeGreaterThan(40)

    await page.getByRole('button', { name: 'toggle rail' }).click()
    await page.waitForTimeout(400)
    const reExpanded = await aside.boundingBox()
    expect(reExpanded?.width).toBeGreaterThan(200)
  })
})

test.describe('login screen', () => {
  test('shows when auth bypass is disabled', async ({ page, context }) => {
    // Override the bypass for this test by clearing the cookie state and
    // hitting an explicit unauthenticated page. Since the bypass is
    // env-gated, we can't easily disable it from a test; instead, just
    // assert the LoginScreen markup is reachable as a component by checking
    // the OAuth route exists (returns JSON).
    const res = await context.request.post(
      '/api/authentication/google/login?redirectTo=%2F'
    )
    expect(res.status()).toBe(200)
    const body = await res.json()
    expect(body.redirectUrl).toContain('accounts.google.com')
  })
})

test.describe('session view', () => {
  test('protected route is reachable', async ({ page }) => {
    // We can't easily create a session without going through the prompt
    // round-trip; smoke-check that an unknown id at least renders the
    // not-found page rather than crashing.
    const res = await page.goto('/s/00000000-0000-0000-0000-000000000000', {
      waitUntil: 'domcontentloaded',
    })
    // Either renders the session shell (header/feed) OR the Next.js 404.
    expect(res?.status()).toBeLessThan(500)
  })
})
