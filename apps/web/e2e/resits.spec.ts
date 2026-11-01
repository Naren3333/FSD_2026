import { test, expect, Page } from '@playwright/test';
import {
  createClassroom,
  deleteTestDataAfterEach,
  enrollStudent,
  ensureTestStudent,
  env,
  expectNoHorizontalScroll,
  login,
  openMenu,
  topicToDelete,
} from './helpers';

deleteTestDataAfterEach();

/* a second local student, who is never assigned the resit. */
const OTHER = { username: 'e2e-other-student', first: 'Other', last: 'Student' };

/* the student picks the classroom and opens its assessment list. */
async function studentAssessments(student: Page, room: string): Promise<void> {
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await student.getByLabel('Current classroom').selectOption({ label: room });
  await student
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Assessments', exact: true })
    .click();
  await student.getByRole('button', { name: 'Refresh assessments' }).click();
}

test('a teacher banks a question and gives one student a resit that only they see', async ({ browser }) => {
  await ensureTestStudent(OTHER.username, OTHER.first, OTHER.last);
  const teacher = await browser.newPage(),
    student = await browser.newPage(),
    other = await browser.newPage();
  await login(student, 'student', env.STUDENT_PASSWORD);
  const studentUsername = await student.locator('.identity-note code').innerText();
  // signing in registers them with identity, so the teacher can enroll them.
  await login(other, OTHER.username, env.STUDENT_PASSWORD);
  await login(teacher, 'teacher', env.TEACHER_PASSWORD);
  const suffix = Date.now().toString();
  const room = 'Resits ' + suffix;
  const title = 'Resit quiz ' + suffix;
  const topic = topicToDelete('Resit topic ' + suffix);
  const prompt = 'What is 7 times 8? (' + suffix + ')';
  await createClassroom(teacher, room);
  await enrollStudent(teacher, studentUsername, 'Student Local', room);
  await enrollStudent(teacher, OTHER.username, 'Other Student', room);
  const menu = teacher.getByRole('navigation', { name: 'Workspace' });
  await menu.getByRole('link', { name: 'Curriculum', exact: true }).click();
  await teacher.getByLabel('Name', { exact: true }).fill(topic);
  await teacher.getByRole('button', { name: 'Add topic', exact: true }).click();
  await expect(teacher.getByText('Topic added.', { exact: true })).toBeVisible();

  // a published assessment, which the student gets wrong.
  await menu.getByRole('link', { name: 'Assessments', exact: true }).click();
  await teacher.locator('#new-assessment').click();
  await teacher.getByLabel('Assessment title').fill(title);
  const first = teacher.getByRole('group', { name: 'Question 1' });
  await first.getByLabel('Question', { exact: true }).fill(prompt);
  await first.getByLabel('Curriculum topic').selectOption({ label: topic });
  await first.getByLabel('Correct answer').fill('56');
  await teacher.getByRole('button', { name: 'Publish assessment', exact: true }).click();
  await expect(teacher.getByText('Assessment published.', { exact: true })).toBeVisible();

  await studentAssessments(student, room);
  await student.getByRole('button', { name: 'Open assessment', exact: true }).click();
  await student.getByLabel(prompt).fill('54');
  await student.getByRole('button', { name: 'Submit answers', exact: true }).click();
  await expect(
    student.getByText('Answers submitted. Your teacher will review the grade.', { exact: true }),
  ).toBeVisible();

  // the teacher finalizes the grade, so the builder can suggest from it.
  await menu.getByRole('link', { name: 'Grading review', exact: true }).click();
  await expect(async () => {
    await teacher.getByRole('button', { name: 'Refresh grades' }).click();
    await expect(teacher.getByRole('button', { name: 'Review grade', exact: true })).toBeVisible({
      timeout: 5000,
    });
  }).toPass({ timeout: 60000 });
  await teacher.getByRole('button', { name: 'Review grade', exact: true }).click();
  await teacher.getByRole('button', { name: 'Finalize this grade', exact: true }).click();
  await teacher.getByLabel('Review notes').fill('Checked the times table answer.');
  await teacher.getByRole('button', { name: 'Finalize grade', exact: true }).click();
  await expect(
    teacher.getByText('Grade finalized. Learning performance will update in a moment.', { exact: true }),
  ).toBeVisible();

  // a submitted assessment's questions are fixed, but each can still go to the bank.
  await menu.getByRole('link', { name: 'Assessments', exact: true }).click();
  await teacher.getByRole('button', { name: 'Refresh assessments' }).click();
  await teacher.getByRole('button', { name: 'Edit details of ' + title, exact: true }).click();
  await teacher.getByRole('button', { name: 'Save to bank question 1', exact: true }).click();
  await expect(teacher.getByText('Question 1 is saved to your bank.', { exact: true })).toBeVisible();
  await teacher.getByRole('link', { name: 'Back to Assessments' }).click();

  // from the assessment's grades, a resit suggested from the finalized grade.
  await menu.getByRole('link', { name: 'Grading review', exact: true }).click();
  await teacher.getByLabel('Assessment', { exact: true }).selectOption({ label: title });
  await teacher.getByRole('link', { name: 'Create resit of ' + title, exact: true }).click();
  await expect(teacher).toHaveURL(/\/assessments\?resit=/);
  await expect(teacher.getByRole('heading', { name: 'Create a resit: ' + title })).toBeVisible();
  const students = teacher.getByRole('list', { name: 'Students' });
  await expect(students.getByRole('checkbox', { name: /Student Local/ })).toBeChecked();
  await expect(students.getByText('Lost marks', { exact: true })).toHaveCount(1);
  await expect(students.getByRole('checkbox', { name: /Other Student/ })).not.toBeChecked();
  const copies = teacher.getByRole('list', { name: 'Questions from the original' });
  await expect(copies.getByRole('checkbox', { name: /What is 7 times 8\?/ })).toBeChecked();
  // with one student chosen, the bank says what they already answered.
  const bank = teacher.getByRole('list', { name: 'Bank questions' });
  await expect(bank.getByText('Already answered', { exact: true })).toBeVisible();
  await teacher.getByLabel('Hide already answered').check();
  await expect(bank.getByText(/has not answered/)).toBeVisible();
  await teacher.getByLabel('Hide already answered').uncheck();
  await expect(bank.getByText('Already answered', { exact: true })).toBeVisible();
  await teacher.screenshot({ path: 'test-results/resit-desktop.png', fullPage: true });
  await expectNoHorizontalScroll(teacher, 'test-results/resit-mobile.png');
  await teacher.setViewportSize({ width: 1280, height: 720 });

  await teacher.getByRole('button', { name: 'Create resit draft', exact: true }).click();
  await expect(teacher).toHaveURL(/\/assessments\?edit=/);
  await expect(teacher.getByRole('heading', { name: 'Edit draft: ' + title + ' (resit)' })).toBeVisible();
  await expect(
    teacher.getByText('Resit draft created for 1 student. Review it, then publish it.', { exact: true }),
  ).toBeVisible();
  await expect(teacher.getByText(/Assigned to 1 student: only they can see it\./)).toBeVisible();
  await teacher.getByRole('button', { name: 'Publish assessment', exact: true }).click();
  await expect(teacher.getByText('Assessment published.', { exact: true })).toBeVisible();
  await expect(teacher.getByText('0 of 1 assigned submitted', { exact: true })).toBeVisible();

  // the assigned student sees it labelled and submits it.
  // back to the list, which the submitted original left open.
  await studentAssessments(student, room);
  const resit = student.getByRole('article').filter({ hasText: title + ' (resit)' });
  await expect(resit.getByText('Resit', { exact: true })).toBeVisible();
  await resit.getByRole('button', { name: 'Open assessment', exact: true }).click();
  await student.getByLabel(prompt).fill('56');
  await student.getByRole('button', { name: 'Submit answers', exact: true }).click();
  await expect(
    student.getByText('Answers submitted. Your teacher will review the grade.', { exact: true }),
  ).toBeVisible();
  await teacher.getByRole('button', { name: 'Refresh assessments' }).click();
  await expect(teacher.getByText('1 of 1 assigned submitted', { exact: true })).toBeVisible();

  // the other student sees the original only.
  await studentAssessments(other, room);
  await expect(other.getByText(title, { exact: true })).toBeVisible();
  await expect(other.getByText(title + ' (resit)', { exact: true })).toHaveCount(0);
  await expect(other.getByText('Resit', { exact: true })).toHaveCount(0);

  // the bank belongs to the teacher, not the classroom, so the test deletes its question itself.
  await openMenu(teacher);
  await menu.getByRole('link', { name: 'Question bank', exact: true }).click();
  await teacher.getByRole('button', { name: 'Refresh question bank' }).click();
  await teacher.getByRole('button', { name: 'Delete ' + prompt, exact: true }).click();
  await teacher.getByRole('dialog').getByRole('button', { name: 'Delete question', exact: true }).click();
  await expect(teacher.getByText('Bank question deleted.', { exact: true })).toBeVisible();
  await teacher.close();
  await student.close();
  await other.close();
});
