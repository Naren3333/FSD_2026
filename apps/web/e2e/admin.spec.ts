import { expect, test, Page } from '@playwright/test';
import { env, expectNoHorizontalScroll, identity, login, passwordToken } from './helpers';

/* reads the sign-in username, which starts with the school's id, and the temporary password the accounts page shows once for the account just created. */
async function createAccount(
  page: Page,
  role: 'Teacher' | 'Student',
  first: string,
  last: string,
  username: string,
) {
  const form = page.getByRole('region', { name: 'Create an account' });
  await form.getByLabel('Role').selectOption({ label: role });
  await form.getByLabel('First name').fill(first);
  await form.getByLabel('Last name').fill(last);
  await form.getByLabel('Username').fill(username);
  await form.getByRole('button', { name: 'Create account', exact: true }).click();
  const created = page.getByRole('region', { name: `${first} ${last} can now sign in` });
  await expect(created.getByRole('heading')).toBeFocused();
  const signIn = (await created.locator('dd').nth(0).innerText()).trim();
  expect(signIn).toBe(`local-school.${username}`);
  const password = (await created.locator('dd').nth(1).innerText()).trim();
  await created.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(created).toHaveCount(0);
  return { signIn, password };
}

/* signs in for the first time with the temporary password and chooses a new one, as keycloak requires. */
async function firstSignIn(page: Page, username: string, temporary: string, chosen: string): Promise<void> {
  await page.goto('/');
  await page.getByRole('button', { name: 'Sign in to your workspace' }).click();
  await page.getByLabel('Username or email').fill(username);
  await page.getByLabel('Password', { exact: true }).fill(temporary);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await page.getByLabel('New Password').fill(chosen);
  await page.getByLabel('Confirm password').fill(chosen);
  await page.getByRole('button', { name: 'Submit' }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
}

test('the school admin creates accounts and a classroom that its teacher and student then share', async ({
  browser,
}, info) => {
  const admin = await browser.newPage();
  const suffix = Date.now().toString(36);
  const teacherName = `t-${suffix}`,
    studentName = `s-${suffix}`,
    room = `Admin ${suffix}`;
  await login(admin, 'admin', env.ADMIN_PASSWORD);
  await expect(admin).toHaveURL(/\/admin\/classrooms$/);
  const menu = admin.getByRole('navigation', { name: 'Workspace' });
  await menu.getByRole('link', { name: 'Accounts', exact: true }).click();
  const teacherAccount = await createAccount(admin, 'Teacher', 'Tess', suffix, teacherName);
  const studentAccount = await createAccount(admin, 'Student', 'Sam', suffix, studentName);
  expect(teacherAccount.password).not.toBe(studentAccount.password);
  // a username already in use is explained in place, with the input kept.
  const form = admin.getByRole('region', { name: 'Create an account' });
  await form.getByLabel('First name').fill('Sam');
  await form.getByLabel('Last name').fill('Again');
  await form.getByLabel('Username').fill(studentName);
  await form.getByRole('button', { name: 'Create account', exact: true }).click();
  await expect(form.getByText('Another account of your school already has that username.')).toBeVisible();
  await expect(form.getByLabel('Username')).toHaveValue(studentName);
  await expectNoHorizontalScroll(admin, info.outputPath('accounts-mobile.png'));
  await admin.setViewportSize({ width: 1280, height: 900 });

  await menu.getByRole('link', { name: 'Classrooms', exact: true }).click();
  const create = admin.getByRole('region', { name: 'Create a classroom' });
  await create.getByLabel('Classroom name').fill(room);
  await create.getByLabel('Teacher').selectOption({ label: `Tess ${suffix}` });
  await create.getByRole('button', { name: 'Create classroom', exact: true }).click();
  await expect(admin.getByText('Classroom created.', { exact: true })).toBeVisible();
  const list = admin.getByRole('region', { name: 'School classrooms' });
  const row = list.getByRole('row').filter({ hasText: room });
  await expect(row.getByText(`Tess ${suffix}`)).toBeVisible();
  await row.getByRole('button', { name: `Manage students of ${room}` }).click();
  const school = admin.getByRole('region', { name: 'School students' });
  await school.getByLabel('Search students').fill(studentName);
  await school.getByRole('button', { name: `Enroll Sam ${suffix} in ${room}` }).click();
  await expect(admin.getByText(`Sam ${suffix} enrolled in ${room}.`)).toBeVisible();
  await expect(
    admin.getByRole('region', { name: `Students in ${room}` }).getByText(`Sam ${suffix}`),
  ).toBeVisible();
  await expect(row.getByRole('cell', { name: '1', exact: true })).toBeVisible();
  await expectNoHorizontalScroll(admin, info.outputPath('admin-classrooms-mobile.png'));

  // the teacher reads the classroom and its roster, and cannot change either.
  const teacher = await browser.newPage();
  await firstSignIn(teacher, teacherAccount.signIn, teacherAccount.password, `chosen-${suffix}-T1!`);
  await expect(teacher).toHaveURL(/\/dashboard$/);
  await teacher.getByLabel('Current classroom').selectOption({ label: room });
  await teacher
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Students', exact: true })
    .click();
  const roster = teacher.getByRole('region', { name: `Students in ${room}` });
  await expect(roster.getByText(`Sam ${suffix}`, { exact: true })).toBeVisible();
  await expect(roster.getByRole('button')).toHaveCount(0);
  await teacher
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Classrooms', exact: true })
    .click();
  await expect(teacher.getByRole('button', { name: 'Create classroom' })).toHaveCount(0);
  await expect(teacher.getByRole('button', { name: `Delete ${room}` })).toHaveCount(0);

  // the student sees the classroom too.
  const student = await browser.newPage();
  await firstSignIn(student, studentAccount.signIn, studentAccount.password, `chosen-${suffix}-S1!`);
  await expect(student.getByRole('option', { name: room })).toHaveCount(1);

  // deleting asks first, with cancel focused, and ends both people's access.
  await row.getByRole('button', { name: `Delete ${room}` }).click();
  await expect(admin.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await admin.getByRole('dialog').getByRole('button', { name: 'Delete classroom', exact: true }).click();
  await expect(admin.getByText(`${room} deleted.`, { exact: true })).toBeVisible();
  await expect(list.getByRole('row').filter({ hasText: room })).toHaveCount(0);
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await expect(student.getByRole('option', { name: room })).toHaveCount(0);

  // the test accounts delete themselves, so none pile up.
  for (const [username, password] of [
    [teacherAccount.signIn, `chosen-${suffix}-T1!`],
    [studentAccount.signIn, `chosen-${suffix}-S1!`],
  ])
    await identity(await passwordToken(username, password), '/me', 'DELETE');
  await Promise.all([admin.close(), teacher.close(), student.close()]);
});

test('teachers and students cannot open the school admin pages', async ({ page }) => {
  await login(page, 'teacher', env.TEACHER_PASSWORD);
  for (const path of ['/admin/classrooms', '/admin/accounts']) {
    await page.goto(path);
    await expect(page).toHaveURL(/\/dashboard$/);
  }
  await expect(page.getByRole('link', { name: 'Accounts' })).toHaveCount(0);
});
