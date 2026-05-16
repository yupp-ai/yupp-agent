import { expect, test } from '@playwright/test'

test('unauthenticated root redirects to /login', async ({ page }) => {
  const response = await page.goto('/')
  expect(response?.status()).toBeLessThan(400)
  await expect(page).toHaveURL(/\/login(\?.*)?$/)
})

test('login page renders sign-in button', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('button', { name: /sign in with google/i })).toBeVisible()
})

test('protected route preserves redirectTo', async ({ page }) => {
  await page.goto('/sessions')
  await expect(page).toHaveURL(/\/login\?redirectTo=%2Fsessions/)
})

test('static asset on login page (logo) loads', async ({ page }) => {
  await page.goto('/login')
  const logo = page.locator('img.logo')
  await expect(logo).toBeVisible()
})
