import { expect, Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

/** The local stack's settings, such as the seeded users' passwords, from the repository's .env. */
export const env: Record<string, string> = Object.fromEntries(
  readFileSync('../../.env', 'utf8')
    .split(/\r?\n/)
    .filter((l: string) => l && !l.startsWith('#'))
    // Only the first '=' separates the name; a value such as a password may contain more.
    .map((l: string) => {
      const at = l.indexOf('=');
      return at < 0 ? [l, ''] : [l.slice(0, at), l.slice(at + 1)];
    }),
);

/** Signs in through Keycloak and waits for the workspace. */
export async function login(page: Page, user: string, password: string): Promise<void> {
  await page.goto('/');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email').fill(user);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
}

/** Creates a classroom from the Classrooms page and makes it the current one. */
export async function createClassroom(page: Page, name: string): Promise<void> {
  await page
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Classrooms', exact: true })
    .click();
  await page.getByLabel('Classroom name', { exact: true }).fill(name);
  await page.getByRole('button', { name: 'Create classroom', exact: true }).click();
  await expect(page.getByText('Classroom created.', { exact: true })).toBeVisible();
  await page.getByLabel('Current classroom').selectOption({ label: name });
}

/** Signs in as the seeded teacher and creates a classroom to work in. */
export async function teacherWithClassroom(page: Page, name: string): Promise<void> {
  await login(page, 'teacher', env.TEACHER_PASSWORD);
  await createClassroom(page, name);
}

/** Narrows the page to a phone's width, saves a screenshot of it, and checks nothing scrolls sideways. */
export async function expectNoHorizontalScroll(page: Page, screenshot: string): Promise<void> {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: screenshot, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}
