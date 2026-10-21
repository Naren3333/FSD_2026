import { test, expect } from '@playwright/test';
import { createClassroom, deleteTestDataAfterEach, env, login, topicToDelete } from './helpers';

deleteTestDataAfterEach();

test('a teacher edits and deletes a published assessment until a student submits it', async ({ browser }) => {
  const teacher = await browser.newPage(),
    student = await browser.newPage();
  await login(student, 'student', env.STUDENT_PASSWORD);
  const studentUsername = await student.locator('.identity-note code').innerText();
  await login(teacher, 'teacher', env.TEACHER_PASSWORD);
  const suffix = Date.now().toString();
  const room = 'Changes ' + suffix;
  await createClassroom(teacher, room);
  const everyone = teacher.getByRole('region', { name: 'All students' });
  await everyone.getByLabel('Search students').fill(studentUsername);
  await everyone.getByRole('button', { name: 'Enroll Student Local in ' + room }).click();
  await expect(teacher.getByText('Student Local enrolled.', { exact: true })).toBeVisible();
  await teacher.getByRole('link', { name: 'Curriculum', exact: true }).click();
  await teacher.getByLabel('Name', { exact: true }).fill(topicToDelete('Changes topic ' + suffix));
  await teacher.getByRole('button', { name: 'Add topic', exact: true }).click();
  await expect(teacher.getByText('Topic added.', { exact: true })).toBeVisible();
  await teacher.getByRole('link', { name: 'Assessments', exact: true }).click();

  // two published assessments: one to edit, one to delete.
  for (const title of ['Quiz ' + suffix, 'Spare ' + suffix]) {
    await teacher.getByLabel('Assessment title').fill(title);
    const first = teacher.getByRole('group', { name: 'Question 1' });
    await first.getByLabel('Question', { exact: true }).fill('What is 2 plus 2?');
    await first.getByLabel('Curriculum topic').selectOption({ label: 'Changes topic ' + suffix });
    await first.getByLabel('Correct answer').fill('4');
    await teacher.getByRole('button', { name: 'Publish assessment', exact: true }).click();
    await expect(teacher.getByText('Assessment published.', { exact: true })).toBeVisible();
  }

  // nobody has submitted yet: the whole assessment can change, and it stays published.
  await teacher.getByRole('button', { name: 'Edit Quiz ' + suffix, exact: true }).click();
  await expect(teacher.getByRole('heading', { name: 'Edit assessment: Quiz ' + suffix })).toBeVisible();
  await expect(teacher.getByRole('button', { name: 'Publish assessment', exact: true })).toHaveCount(0);
  await teacher.getByLabel('Assessment title').fill('Sums ' + suffix);
  await teacher
    .getByRole('group', { name: 'Question 1' })
    .getByLabel('Question', { exact: true })
    .fill('What is 3 plus 3?');
  await teacher.getByLabel('Correct answer').fill('6');
  await teacher.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(teacher.getByText('Sums ' + suffix + ' saved.', { exact: true })).toBeVisible();

  await teacher.getByRole('button', { name: 'Delete Spare ' + suffix, exact: true }).click();
  await expect(teacher.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await teacher.getByRole('dialog').getByRole('button', { name: 'Delete assessment', exact: true }).click();
  await expect(teacher.getByText('Spare ' + suffix + ' deleted.', { exact: true })).toBeVisible();
  await expect(teacher.getByText('Spare ' + suffix, { exact: true })).toHaveCount(0);

  // the student answers the changed question.
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await student.getByLabel('Current classroom').selectOption({ label: room });
  await student.getByRole('link', { name: 'Assessments', exact: true }).click();
  await expect(student.getByText('Spare ' + suffix, { exact: true })).toHaveCount(0);
  await student.getByRole('button', { name: 'Open assessment', exact: true }).click();
  await student.getByLabel('What is 3 plus 3?').fill('6');
  await student.getByRole('button', { name: 'Submit answers', exact: true }).click();
  await expect(
    student.getByText('Answers submitted. Your teacher will review the grade.', { exact: true }),
  ).toBeVisible();

  // once submitted, only the title, due date and late policy change.
  await teacher.getByRole('button', { name: 'Refresh assessments' }).click();
  await expect(teacher.getByText(/1 submitted/)).toBeVisible();
  await teacher.getByRole('button', { name: 'Edit details of Sums ' + suffix, exact: true }).click();
  await expect(teacher.getByRole('group', { name: 'Question 1' })).toHaveCount(0);
  await expect(teacher.getByLabel('When students see grades')).toHaveCount(0);
  await teacher.getByLabel('Assessment title').fill('Sums marked ' + suffix);
  await teacher.getByLabel('After the due date').selectOption('reject');
  await teacher.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(teacher.getByText('Sums marked ' + suffix + ' saved.', { exact: true })).toBeVisible();
  await expect(teacher.locator('strong').getByText('Sums marked ' + suffix, { exact: true })).toBeVisible();

  // the teacher's preview closes from its own button, and focus goes back to the button that opened it.
  const preview = teacher.getByRole('button', { name: 'Preview assessment', exact: true });
  await preview.click();
  await expect(teacher.getByText(/Teacher preview\./)).toBeVisible();
  // not the list's toggle, which also reads close preview while the preview is open.
  await teacher.locator('#answer-form').getByRole('button', { name: 'Close preview', exact: true }).click();
  await expect(teacher.getByText(/Teacher preview\./)).toHaveCount(0);
  await expect(preview).toBeFocused();

  // deleting it now takes the student's submission and grade too, which the dialog says first.
  await teacher.getByRole('button', { name: 'Delete Sums marked ' + suffix, exact: true }).click();
  const dialog = teacher.getByRole('dialog');
  await expect(dialog.getByRole('heading', { name: 'Delete this assessment and its grades?' })).toBeVisible();
  await expect(dialog.getByText(/the 1 submission a student\s+made/)).toBeVisible();
  await expect(teacher.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await dialog.getByRole('button', { name: 'Delete assessment and grades', exact: true }).click();
  await expect(teacher.getByText('Sums marked ' + suffix + ' deleted.', { exact: true })).toBeVisible();
  await expect(teacher.getByText('No assessments yet. Create one using a curriculum topic.')).toBeVisible();
  await student.getByRole('button', { name: 'Refresh assessments' }).click();
  await expect(student.getByText('No assessments yet. Your teacher has not published any.')).toBeVisible();
});
