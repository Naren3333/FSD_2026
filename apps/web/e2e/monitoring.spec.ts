import { expect, test } from '@playwright/test';
import { env, expectNoHorizontalScroll, login } from './helpers';

test('a school admin opens monitoring without Identity and can recover each section', async ({
  page,
}, info) => {
  await page.route('**/api/identity/**', (route) => route.abort());
  await login(page, 'admin', env.ADMIN_PASSWORD);
  await expect(page).toHaveURL(/\/admin\/classrooms$/);
  const menu = page.getByRole('navigation', { name: 'Workspace' });
  await expect(menu.getByRole('link')).toHaveText(['Classrooms', 'Accounts', 'Monitoring']);
  await expect(page.getByLabel('Current classroom')).toHaveCount(0);
  // monitoring needs no identity call, so it works while identity is down.
  const identityCalls: string[] = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/identity/')) identityCalls.push(request.url());
  });
  await menu.getByRole('link', { name: 'Monitoring', exact: true }).click();
  await expect(page).toHaveURL(/\/admin\/monitoring$/);
  await expect(page.getByRole('heading', { name: 'Monitoring', exact: true })).toBeVisible();
  const services = page.getByRole('region', { name: 'Services' });
  const dlq = page.getByRole('region', { name: 'Dead letters' });
  const spend = page.getByRole('region', { name: 'AI expenditure' });
  await expect(services.getByRole('rowheader', { name: 'identity', exact: true })).toBeVisible();
  await expect(dlq.getByRole('rowheader', { name: 'assessment.submitted.v4.dlq' })).toBeVisible();
  await expect(spend.getByText('Recorded today', { exact: true })).toBeVisible();
  expect(identityCalls).toEqual([]);

  await page.route('**/api/monitoring/status', (route) =>
    route.fulfill({ status: 503, json: { error: { code: 'UNAVAILABLE', message: 'Status read failed.' } } }),
  );
  const refreshStatus = services.getByRole('button', { name: 'Refresh services and dead letters' });
  await refreshStatus.click();
  await expect(
    page.getByRole('alert').filter({ hasText: 'Showing stale data. Status read failed.' }),
  ).toHaveCount(1);
  await expect(services.getByRole('table')).toBeVisible();
  await expect(dlq.getByRole('table')).toBeVisible();
  await page.unroute('**/api/monitoring/status');
  await refreshStatus.click();
  await expect(services.getByRole('alert')).toHaveCount(0);

  await page.route('**/api/monitoring/ai-spend', (route) =>
    route.fulfill({ status: 503, json: { error: { code: 'UNAVAILABLE', message: 'Spend read failed.' } } }),
  );
  await spend.getByRole('button', { name: 'Refresh AI expenditure' }).click();
  await expect(spend.getByRole('alert')).toContainText('Showing stale data. Spend read failed.');
  await expect(services.getByRole('table')).toBeVisible();
  await expect(dlq.getByRole('table')).toBeVisible();
  await page.unroute('**/api/monitoring/ai-spend');
  await spend.getByRole('button', { name: 'Refresh AI expenditure' }).click();
  await expect(spend.getByRole('alert')).toHaveCount(0);

  const filter = page.getByLabel('Billed user ID');
  const usersPath = (url: URL) => url.pathname === '/api/monitoring/ai-spend/users';
  let releaseRefresh = () => {};
  const held = new Promise<void>((resolve) => {
    releaseRefresh = resolve;
  });
  let finishRefresh = () => {};
  const oldResponse = new Promise<void>((resolve) => {
    finishRefresh = resolve;
  });
  await page.route(usersPath, async (route) => {
    if (new URL(route.request().url()).searchParams.has('user_id')) {
      await route.continue();
      return;
    }
    await held;
    try {
      await route.fulfill({
        json: {
          as_of: new Date().toISOString(),
          timezone: 'Asia/Singapore',
          total: 1,
          items: [
            { user_id: 'old-result', org_id: 'old-org', recorded_usd: '1', estimated_usd: '0', calls: 1 },
          ],
        },
      });
    } catch {
      // the newer filter may have already canceled this request.
    } finally {
      finishRefresh();
    }
  });
  try {
    const refreshing = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return usersPath(url) && !url.searchParams.has('user_id');
    });
    await spend.getByRole('button', { name: 'Refresh billed accounts' }).click();
    await refreshing;
    const filtered = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return usersPath(url) && url.searchParams.get('user_id') === 'does-not-exist';
    });
    await filter.fill('does-not-exist');
    await filter.press('Enter');
    expect((await filtered).ok()).toBe(true);
    await expect(spend.getByText('No billed accounts match these filters.')).toBeVisible();
  } finally {
    releaseRefresh();
  }
  await oldResponse;
  await page.unroute(usersPath);
  await expect(spend.getByText('No billed accounts match these filters.')).toBeVisible();
  await expect(spend.getByRole('rowheader', { name: 'old-result' })).toHaveCount(0);
  await expectNoHorizontalScroll(page, info.outputPath('monitoring-mobile.png'));

  await page.route('**/api/monitoring/status', (route) =>
    route.fulfill({
      status: 403,
      json: { error: { code: 'FORBIDDEN', message: 'Operator role required.' } },
    }),
  );
  await services.getByRole('button', { name: 'Refresh services and dead letters' }).click();
  await expect(page.getByRole('alert')).toHaveText('Your account cannot access monitoring.');
  await expect(spend).toHaveCount(0);
  await expect(services).toHaveCount(0);
});

test('a teacher who opens the monitoring address returns to the learning workspace', async ({ page }) => {
  await login(page, 'teacher', env.TEACHER_PASSWORD);
  await page.goto('/admin/monitoring');
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByRole('link', { name: 'Monitoring' })).toHaveCount(0);
});
