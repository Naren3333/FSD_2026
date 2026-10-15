import { test, expect, Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
const env = Object.fromEntries(
  readFileSync('../../.env', 'utf8')
    .split(/\r?\n/)
    .filter((l) => l && !l.startsWith('#'))
    .map((l) => l.split('=')),
);
async function login(page: Page, user: string, password: string) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByLabel('Username or email').fill(user);
  await page.getByLabel('Password', { exact: true }).fill(password);
  await page.getByRole('button', { name: 'Sign In', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible();
}
test('teacher and student complete a real assessment through Kong', async ({ browser }) => {
  const teacher = await browser.newPage(),
    student = await browser.newPage();
  await login(student, 'student', env.STUDENT_PASSWORD);
  const studentUsername = await student.locator('.identity-note code').innerText();
  // Each role lands on its own page and sees only its own menu.
  await expect(student).toHaveURL(/\/home$/);
  const studentMenu = student.getByRole('navigation', { name: 'Workspace' });
  await expect(studentMenu.getByRole('link', { name: 'Classrooms', exact: true })).toHaveCount(0);
  await expect(studentMenu.getByRole('link', { name: 'Curriculum', exact: true })).toHaveCount(0);
  await login(teacher, 'teacher', env.TEACHER_PASSWORD);
  await expect(teacher).toHaveURL(/\/dashboard$/);
  await expect(teacher.locator('.identity-note')).toHaveCount(0);
  await expect(teacher.getByRole('link', { name: 'My results', exact: true })).toHaveCount(0);
  await expect(studentMenu.getByRole('link', { name: 'Class dashboard', exact: true })).toHaveCount(0);
  // The dashboard links to Classrooms too, so the menu link is the one to use.
  const teacherMenu = teacher.getByRole('navigation', { name: 'Workspace' });
  await teacherMenu.getByRole('link', { name: 'Classrooms', exact: true }).click();
  const suffix = Date.now().toString();
  await teacher.getByLabel('Classroom name', { exact: true }).fill('Browser ' + suffix);
  await teacher.getByRole('button', { name: 'Create classroom', exact: true }).click();
  await expect(teacher.getByText('Classroom created.', { exact: true })).toBeVisible();
  await teacher.getByLabel('Current classroom').selectOption({ label: 'Browser ' + suffix });
  // The teacher finds the student in the school's list by username and enrolls them.
  const everyone = teacher.getByRole('region', { name: 'All students' });
  await everyone.getByLabel('Search students').fill(studentUsername);
  await everyone.getByRole('button', { name: 'Enroll Student Local in Browser ' + suffix }).click();
  await expect(teacher.getByText('Student Local enrolled.', { exact: true })).toBeVisible();
  const roster = teacher.getByRole('region', { name: 'Students in Browser ' + suffix });
  await expect(roster.getByText('Student Local', { exact: true })).toBeVisible();
  await expect(
    everyone.getByRole('article').filter({ hasText: 'Student Local' }).getByText('Enrolled', { exact: true }),
  ).toBeVisible();
  await teacher.getByRole('link', { name: 'Curriculum', exact: true }).click();
  await teacher.getByLabel('Name', { exact: true }).fill('Browser skill ' + suffix);
  await teacher.getByRole('button', { name: 'Add curriculum', exact: true }).click();
  await expect(teacher.getByText('Curriculum added.', { exact: true })).toBeVisible();
  await teacher.getByRole('link', { name: 'Assessments', exact: true }).click();
  await teacher.getByRole('button', { name: 'Refresh assessments' }).click();
  await teacher.getByLabel('Assessment title').fill('Browser assessment ' + suffix);
  await teacher.getByLabel('Question', { exact: true }).fill('What is 0.1 plus 0.2?');
  await teacher.getByLabel('Curriculum skill').selectOption({ label: 'Browser skill ' + suffix });
  await teacher.getByLabel('Correct answer').fill('0.3');
  await teacher.getByRole('button', { name: 'Create assessment', exact: true }).click();
  await expect(teacher.getByText('Assessment created.', { exact: true })).toBeVisible();
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await student.getByLabel('Current classroom').selectOption({ label: 'Browser ' + suffix });
  // Home lists the new assessment as to do and opens it.
  const todo = student.getByRole('region', { name: 'To do' });
  await todo.getByRole('link', { name: 'Start Browser assessment ' + suffix }).click();
  await expect(student).toHaveURL(/\/assessments\?open=/);
  await student.getByLabel('What is 0.1 plus 0.2?').fill('0.30');
  await student.getByRole('button', { name: 'Submit answers', exact: true }).click();
  await expect(
    student.getByText('Answers submitted. Your teacher will review the grade.', { exact: true }),
  ).toBeVisible();
  await student.getByRole('link', { name: 'Home', exact: true }).click();
  await student.getByRole('button', { name: 'Refresh home' }).click();
  await expect(todo.getByText(/All caught up/)).toBeVisible();
  await expect(student.getByText(/1 submission is waiting for your teacher's review/)).toBeVisible();
  await teacher.getByRole('link', { name: 'Grading review', exact: true }).click();
  await expect(async () => {
    await teacher.getByRole('button', { name: 'Refresh grades' }).click();
    await expect(teacher.getByRole('button', { name: 'Review grade', exact: true })).toBeVisible();
  }).toPass({ timeout: 30000 });
  // The review shows names, not ids: the student, the assessment, the question and its skill.
  await expect(teacher.getByRole('heading', { name: 'Student Local', exact: true })).toBeVisible();
  await expect(teacher.getByText('Browser assessment ' + suffix, { exact: false })).toBeVisible();
  await expect(teacher.getByText('What is 0.1 plus 0.2?', { exact: false })).toBeVisible();
  await expect(teacher.getByText('Skill: Browser skill ' + suffix, { exact: true })).toBeVisible();
  // A draft is the teacher's alone until it is finalized.
  await student.getByRole('link', { name: 'My results', exact: true }).click();
  await student.getByRole('button', { name: 'Refresh results' }).click();
  await expect(student.getByText(/No finalized results yet/)).toBeVisible();
  await teacher.getByRole('button', { name: 'Review grade', exact: true }).click();
  await teacher.getByLabel('Review notes').fill('Reviewed exact decimal answer.');
  await teacher.getByRole('button', { name: 'Finalize grade', exact: true }).click();
  await expect(
    teacher.getByText('Grade finalized. Performance will update after event processing.', { exact: true }),
  ).toBeVisible();
  await student.getByRole('button', { name: 'Refresh results' }).click();
  await expect(
    student.getByRole('heading', { name: 'Browser assessment ' + suffix, exact: true }),
  ).toBeVisible();
  await expect(student.getByText('Correct', { exact: true })).toBeVisible();
  await expect(student.getByText('Skill: Browser skill ' + suffix, { exact: true })).toBeVisible();
  await student.getByRole('link', { name: 'My progress', exact: true }).click();
  await expect(async () => {
    await student.getByRole('button', { name: 'View my performance' }).click();
    await expect(student.getByRole('cell', { name: '100%' })).toBeVisible();
  }).toPass({ timeout: 30000 });
  await expect(student.getByRole('cell', { name: 'Insufficient evidence' })).toBeVisible();
  await expect(
    student.getByRole('rowheader', { name: 'Browser skill ' + suffix, exact: true }),
  ).toBeVisible();
  // The class dashboard counts the finalized grade and shows the class's skills; a student's name opens their
  // full performance.
  await teacher.getByRole('link', { name: 'Class dashboard', exact: true }).click();
  const skills = teacher.getByRole('region', { name: 'Class skills table' });
  await expect(async () => {
    await teacher.getByRole('button', { name: 'Refresh dashboard' }).click();
    await expect(
      skills.getByRole('rowheader', { name: 'Browser skill ' + suffix, exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 30000 });
  await expect(teacher.getByText('1 of 1 students submitted', { exact: true })).toBeVisible();
  await expect(skills.getByText('100%', { exact: true })).toBeVisible();
  await skills.getByRole('link', { name: 'Student Local', exact: true }).click();
  await expect(teacher).toHaveURL(/\/performance\?student=/);
  await expect(teacher.getByLabel('Student', { exact: true })).toHaveValue(/.+/);
  await expect(teacher.getByRole('cell', { name: '100%' })).toBeVisible();
  await student.screenshot({ path: 'test-results/performance-desktop.png', fullPage: true });
  await student.setViewportSize({ width: 390, height: 844 });
  await student.screenshot({ path: 'test-results/performance-mobile.png', fullPage: true });
  expect(await student.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  // Removing asks first, with Cancel focused; the student then no longer sees the classroom.
  await teacherMenu.getByRole('link', { name: 'Classrooms', exact: true }).click();
  await roster.getByRole('button', { name: 'Remove Student Local from the classroom' }).click();
  await expect(teacher.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await teacher.getByRole('button', { name: 'Remove student', exact: true }).click();
  await expect(teacher.getByText('Student removed from the classroom.', { exact: true })).toBeVisible();
  await expect(roster.getByText(/No students enrolled yet/)).toBeVisible();
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await expect(student.getByRole('option', { name: 'Browser ' + suffix })).toHaveCount(0);
  await teacher.close();
  await student.close();
});

test('anonymous workspace has honest unavailable state and keyboard navigation', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.getByText(/AI-generated questions and feedback are not implemented/)).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to workspace' })).toBeFocused();
  await page.screenshot({ path: 'test-results/welcome.png', fullPage: true });
});
