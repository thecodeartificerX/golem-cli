import { test, expect } from '@playwright/test';

const BASE = 'http://127.0.0.1:7665';

test('BROWSE shows dropdown with spec files', async ({ page }) => {
  await page.goto(BASE);
  await page.waitForSelector('#browse-btn');

  await page.click('#browse-btn');
  await expect(page.locator('#spec-dropdown')).toBeVisible({ timeout: 3000 });

  // Should have at least one .spec-item
  const items = page.locator('#spec-dropdown .spec-item');
  await expect(items.first()).toBeVisible({ timeout: 3000 });
});

test('clicking a spec file sets input and closes dropdown', async ({ page }) => {
  await page.goto(BASE);
  await page.click('#browse-btn');
  await expect(page.locator('#spec-dropdown')).toBeVisible({ timeout: 3000 });

  const firstItem = page.locator('#spec-dropdown .spec-item').first();
  const itemText = await firstItem.textContent();

  // Skip if it's a "no files" or "loading" message
  if (itemText && itemText.includes('/')) {
    await firstItem.click();
    await page.waitForTimeout(200);

    // Dropdown should close
    await expect(page.locator('#spec-dropdown')).toBeHidden();

    // Input should have full path
    const val = await page.inputValue('#spec-path-input');
    expect(val).toContain('/');

    // CONSTRUCT button should be enabled
    await expect(page.locator('#run-btn')).toBeEnabled();
  }
});

test('clicking outside closes dropdown', async ({ page }) => {
  await page.goto(BASE);
  await page.click('#browse-btn');
  await expect(page.locator('#spec-dropdown')).toBeVisible({ timeout: 3000 });

  // Click the page body
  await page.click('body', { position: { x: 10, y: 300 } });
  await page.waitForTimeout(200);
  await expect(page.locator('#spec-dropdown')).toBeHidden();
});

test('/api/specs returns valid spec list', async ({ page }) => {
  await page.goto(BASE);
  const res = await page.evaluate(async () => {
    const r = await fetch('/api/specs');
    return { status: r.status, data: await r.json() };
  });
  expect(res.status).toBe(200);
  expect(Array.isArray(res.data.specs)).toBe(true);
  expect(res.data.specs.length).toBeGreaterThan(0);

  // Each spec should be a full path containing /
  res.data.specs.forEach(s => {
    expect(s).toContain('/');
    expect(s.endsWith('.md')).toBe(true);
  });
});
