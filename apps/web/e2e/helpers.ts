import { expect, test, Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';

/* read local settings with the same whitespace and quoting rules as compose. */
export const env: Record<string, string> = Object.fromEntries(
  readFileSync('../../.env', 'utf8')
    .split(/\r?\n/)
    .map((l: string) => l.trim())
    .filter((l: string) => l && !l.startsWith('#') && l.includes('='))
    // split only on the first equals sign; passwords may contain more.
    .map((l: string) => {
      const at = l.indexOf('=');
      const name = l
        .slice(0, at)
        .trim()
        .replace(/^export\s+/, '');
      const value = l.slice(at + 1).trim();
      const quoted = value.length >= 2 && (value[0] === '"' || value[0] === "'") && value.at(-1) === value[0];
      return [name, quoted ? value.slice(1, -1) : value];
    }),
);

/* signs in through keycloak and waits for the workspace. */
export async function login(page: Page, user: string, password: string): Promise<void> {
  await page.goto('/');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email').fill(user);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
}

/* creates a classroom from the classrooms page and makes it the current one. */
export async function createClassroom(page: Page, name: string): Promise<void> {
  await page
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Classrooms', exact: true })
    .click();
  await page.getByLabel('Classroom name', { exact: true }).fill(name);
  const created = page.waitForResponse(
    (r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/identity/classrooms',
  );
  await page.getByRole('button', { name: 'Create classroom', exact: true }).click();
  const response = await created;
  if (response.status() === 201) createdClassrooms.push(((await response.json()) as { id: string }).id);
  await expect(page.getByText('Classroom created.', { exact: true })).toBeVisible();
  await page.getByLabel('Current classroom').selectOption({ label: name });
}

/* signs in as the seeded teacher and creates a classroom to work in. */
export async function teacherWithClassroom(page: Page, name: string): Promise<void> {
  await login(page, 'teacher', env.TEACHER_PASSWORD);
  await createClassroom(page, name);
}

/* narrows the page to a phone's width, saves a screenshot of it, and checks nothing scrolls sideways. */
export async function expectNoHorizontalScroll(page: Page, screenshot: string): Promise<void> {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: screenshot, fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
}

/* archives a topic from the curriculum page, through its confirmation dialog. */
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
  await expect(page.getByRole('button', { name: 'Restore topic ' + name, exact: true })).toBeVisible();
}

/* restores an archived topic from the curriculum page, which must be open. */
export async function restoreTopic(page: Page, name: string): Promise<void> {
  await page.getByRole('button', { name: 'Restore topic ' + name, exact: true }).click();
  await expect(page.getByText(name + ' restored.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Restore topic ' + name, exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Archive topic ' + name, exact: true })).toBeVisible();
}

/* deletes a topic a test created, archived or not, straight from curriculum's own database in the local stack, so test topics never pile up. */
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

/* the stack's kong gateway and keycloak, on the ports scripts/local_stack.py reads: the environment's (stack_tests.py sets the test stack's), else .env's, else the development defaults. */
const GATEWAY = `http://127.0.0.1:${process.env['KONG_PORT'] || env['KONG_PORT'] || '8000'}/api`;
const KEYCLOAK = `http://127.0.0.1:${process.env['KEYCLOAK_PORT'] || env['KEYCLOAK_PORT'] || '8081'}`;

/* deletes a classroom a test created through identity's api, as the seeded teacher who created it, so every service purges its data for it as for a teacher's deletion and test classrooms never pile up. */
export async function deleteTestClassroom(id: string): Promise<void> {
  const signIn = await fetch(`${KEYCLOAK}/realms/learning/protocol/openid-connect/token`, {
    method: 'POST',
    body: new URLSearchParams({
      client_id: 'learning-e2e',
      grant_type: 'password',
      username: 'teacher',
      password: env.TEACHER_PASSWORD,
    }),
  });
  if (!signIn.ok) throw new Error(`Teacher sign-in to delete classroom ${id} failed: ${signIn.status}`);
  const { access_token } = (await signIn.json()) as { access_token: string };
  const deleted = await fetch(`${GATEWAY}/identity/classrooms/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: { Authorization: 'Bearer ' + access_token },
  });
  if (!deleted.ok && deleted.status !== 403) {
    throw new Error(`Deleting classroom ${id} failed: ${deleted.status} ${await deleted.text()}`);
  }
}

/* classrooms (by id) and topics (by name) the running test created, deleted after it whether it passed or failed (see deletetestdataaftereach). */
const createdClassrooms: string[] = [];
const createdTopics: string[] = [];

/* records a topic the test is about to create, so it is deleted after the test even if the test fails. */
export function topicToDelete(name: string): string {
  createdTopics.push(name);
  return name;
}

/* call once at the top of a spec that creates classrooms or topics. */
export function deleteTestDataAfterEach(): void {
  test.afterEach(async () => {
    for (const id of createdClassrooms.splice(0)) await deleteTestClassroom(id);
    for (const name of createdTopics.splice(0)) deleteTestTopic(name);
  });
}
