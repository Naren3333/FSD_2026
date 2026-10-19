import { expect, test } from '@playwright/test';
import { deleteTestDataAfterEach, expectNoHorizontalScroll, teacherWithClassroom } from './helpers';

deleteTestDataAfterEach();

test('materials page through real data, recover from a failed read and fall back after deletion', async ({
  page,
}) => {
  await teacherWithClassroom(page, 'Paged materials ' + Date.now());
  const reading = page.waitForRequest((request) => request.url().includes('/api/content/documents/page?'));
  await page
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Teaching materials' })
    .click();
  const request = await reading;
  const headers = await request.allHeaders();
  const classroom = new URL(request.url()).searchParams.get('classroom_id')!;
  const origin = new URL(request.url()).origin;
  for (let i = 0; i < 22; i++) {
    const response = await page.request.post(
      `${origin}/api/content/documents?classroom_id=${classroom}&title=Material%20${i}`,
      {
        headers: { Authorization: headers['authorization'], 'Content-Type': 'text/plain' },
        data: 'A real teaching material for the pagination test.',
      },
    );
    expect(response.status()).toBe(201);
  }
  await page.getByRole('button', { name: 'Refresh materials' }).click();
  const pages = page.getByRole('navigation', { name: 'Materials pages' });
  await expect(pages).toContainText('Page 1 of 2');
  await pages.getByRole('button', { name: 'Next' }).focus();
  await page.keyboard.press('Enter');
  await expect(pages).toContainText('Page 2 of 2');
  await expect(page.locator('article.record')).toHaveCount(2);
  await page.route('**/api/content/documents/page*', (route) => route.abort('failed'));
  await page.getByRole('button', { name: 'Refresh materials' }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'could not be reached' })).toBeVisible();
  await page.unroute('**/api/content/documents/page*');
  await page.getByRole('button', { name: 'Refresh materials' }).click();
  await expect(page.locator('article.record')).toHaveCount(2);
  for (let i = 0; i < 2; i++) {
    await page
      .locator('article.record')
      .first()
      .getByRole('button', { name: /^Delete/ })
      .click();
    await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete document', exact: true }).click();
    await expect(page.getByRole('dialog')).not.toBeVisible();
    await expect(page.locator('article.record')).toHaveCount(i === 0 ? 1 : 20);
  }
  await expect(pages).not.toBeVisible();
  await expectNoHorizontalScroll(page, test.info().outputPath('materials-pagination-mobile.png'));
});
