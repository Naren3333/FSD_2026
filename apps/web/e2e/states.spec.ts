import { test, expect } from '@playwright/test';
import { env, login } from './helpers';

test('forms preserve drafts and list refresh exposes loading and network recovery', async ({ page }) => {
  await login(page, 'teacher', env.TEACHER_PASSWORD);
  await page
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Classrooms', exact: true })
    .click();
  const name = page.getByLabel('Classroom name', { exact: true });
  await page.getByRole('button', { name: 'Create classroom', exact: true }).click();
  await expect(name).toHaveAttribute('aria-invalid', 'true');
  await expect(name).toBeFocused();
  await name.fill('Unsaved classroom');
  await expect(page.getByLabel('Current classroom')).toBeDisabled();
  await page.locator('form').filter({ has: name }).getByRole('button', { name: 'Clear form' }).click();
  await expect(name).toHaveValue('');
  await expect(page.getByLabel('Current classroom')).toBeEnabled();
  let release!: () => void;
  const pending = new Promise<void>((resolve) => (release = resolve));
  await page.route('**/api/identity/classrooms', async (route) => {
    await pending;
    await route.abort('failed');
  });
  await page.getByRole('button', { name: 'Refresh classrooms' }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Working…' })).toBeVisible();
  release();
  await expect(
    page.getByRole('alert').filter({ hasText: 'The service could not be reached.' }),
  ).toBeVisible();
  await page.unroute('**/api/identity/classrooms');
  await page.getByRole('button', { name: 'Refresh classrooms' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'The service could not be reached.' })).toHaveCount(
    0,
  );
  await expect(page.getByRole('status').filter({ hasText: 'Working…' })).toHaveCount(0);
});
