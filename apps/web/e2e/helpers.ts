import { expect, test, Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
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

/** Archives a topic from the Curriculum page, through its confirmation dialog. */
export async function archiveTopic(page: Page, name: string): Promise<void> {
  await page
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Curriculum', exact: true })
    .click();
  await page.getByRole('button', { name: 'Archive topic ' + name, exact: true }).click();
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await page.getByRole('dialog').getByRole('button', { name: 'Archive', exact: true }).click();
  await expect(page.getByText(name + ' archived.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Archive topic ' + name, exact: true })).toHaveCount(0);
}

/** Deletes a topic a test created, archived or not, straight from Curriculum's own database in the local stack, so
 * test topics never pile up. Only for test data: anything of the test's that names the topic shows its id. The
 * statement is scripts/local_stack.py delete_topics's. */
export function deleteTestTopic(name: string): void {
  const quoted = name.replaceAll("'", "''");
  const ids = `(SELECT id FROM topics WHERE name='${quoted}')`;
  const sql = `WITH p AS (DELETE FROM prerequisites WHERE topic_id IN ${ids} OR prerequisite_id IN ${ids}), d AS (DELETE FROM topics WHERE id IN ${ids} RETURNING 1) SELECT count(*) FROM d`;
  execFileSync(
    'docker',
    [
      'compose',
      'exec',
      '-T',
      'postgres',
      'psql',
      '-U',
      'curriculum',
      '-d',
      'curriculum',
      '-v',
      'ON_ERROR_STOP=1',
      '-c',
      sql,
    ],
    { cwd: '../..', stdio: 'pipe', timeout: 60000 },
  );
}

/** Topics the running test created, deleted after it whether it passed or failed (see deleteTopicsAfterEach). */
const createdTopics: string[] = [];

/** Records a topic the test is about to create, so it is deleted after the test even if the test fails. */
export function topicToDelete(name: string): string {
  createdTopics.push(name);
  return name;
}

/** Call once at the top of a spec that creates topics. */
export function deleteTopicsAfterEach(): void {
  test.afterEach(() => {
    for (const name of createdTopics.splice(0)) deleteTestTopic(name);
  });
}
