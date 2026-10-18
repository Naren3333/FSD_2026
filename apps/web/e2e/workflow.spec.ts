import { test, expect } from '@playwright/test';
import {
  createClassroom,
  deleteTopicsAfterEach,
  env,
  expectNoHorizontalScroll,
  login,
  topicToDelete,
} from './helpers';

deleteTopicsAfterEach();

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
  const suffix = Date.now().toString();
  await createClassroom(teacher, 'Browser ' + suffix);
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
  await teacher.getByLabel('Name', { exact: true }).fill(topicToDelete('Browser topic ' + suffix));
  await teacher.getByRole('button', { name: 'Add topic', exact: true }).click();
  await expect(teacher.getByText('Topic added.', { exact: true })).toBeVisible();
  await teacher.getByRole('link', { name: 'Assessments', exact: true }).click();
  await teacher.getByRole('button', { name: 'Refresh assessments' }).click();
  await teacher.getByLabel('Assessment title').fill('Browser assessment ' + suffix);
  const first = teacher.getByRole('group', { name: 'Question 1' });
  await first.getByLabel('Question', { exact: true }).fill('What is 0.1 plus 0.2?');
  await first.getByLabel('Curriculum topic').selectOption({ label: 'Browser topic ' + suffix });
  await first.getByLabel('Correct answer').fill('0.3');
  // A written response is marked by the teacher against its rubric; the new question keeps the topic.
  await teacher.getByRole('button', { name: 'Add question', exact: true }).click();
  const second = teacher.getByRole('group', { name: 'Question 2' });
  await expect(second.getByLabel('Question', { exact: true })).toBeFocused();
  await second.getByLabel('Question', { exact: true }).fill('Why is 0.1 plus 0.2 not exactly 0.3 in binary?');
  await second.getByLabel('Question type').selectOption('free_response');
  await second.getByLabel('Criterion 1', { exact: true }).fill('Reasoning');
  await second.getByLabel('Criterion 1 levels').fill('0: Missing\n2: Explains rounding');
  await expect(second.getByLabel('Marks')).toHaveValue('2');
  // A pasted marking-scheme passage, for AI suggestions only; this classroom has no indexed marking scheme.
  const scheme = second.getByRole('group', { name: 'Marking scheme (optional)' });
  await expect(scheme.getByText(/No indexed marking scheme or rubric in this classroom/)).toBeVisible();
  await scheme.getByLabel('Paste a passage').fill('Full marks need rounding in binary named.');
  await scheme.getByRole('button', { name: 'Add pasted passage to question 2' }).click();
  await expect(scheme.getByText('Pasted passage', { exact: true })).toBeVisible();
  await expect(scheme.getByRole('button', { name: 'Remove passage 1 from question 2' })).toBeVisible();
  await teacher.screenshot({ path: 'test-results/editor-desktop.png', fullPage: true });
  // A draft stays with the teacher until it is published.
  await teacher.getByRole('button', { name: 'Create draft', exact: true }).click();
  await expect(teacher.getByText('Draft created.', { exact: true })).toBeVisible();
  await expect(
    teacher.getByRole('heading', { name: 'Edit draft: Browser assessment ' + suffix }),
  ).toBeVisible();
  await teacher.getByRole('button', { name: 'Publish assessment', exact: true }).click();
  await expect(teacher.getByText('Assessment published.', { exact: true })).toBeVisible();
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await student.getByLabel('Current classroom').selectOption({ label: 'Browser ' + suffix });
  // Home lists the new assessment as to do and opens it.
  const todo = student.getByRole('region', { name: 'To do' });
  await todo.getByRole('link', { name: 'Start Browser assessment ' + suffix }).click();
  await expect(student).toHaveURL(/\/assessments\?open=/);
  await student.getByLabel('What is 0.1 plus 0.2?').fill('0.30');
  await expect(student.getByText('Full marks need rounding in binary named.')).toHaveCount(0);
  await student
    .getByLabel('Why is 0.1 plus 0.2 not exactly 0.3 in binary?')
    .fill('Neither number has an exact binary form, so both are rounded.');
  // Work is saved as the student types.
  await expect(student.getByText(/^Saved at /)).toBeVisible();
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
  // The review shows names, not ids: the student, the assessment, the question and its topic.
  await expect(teacher.getByRole('heading', { name: 'Student Local', exact: true })).toBeVisible();
  await expect(
    teacher.locator('article').getByText('Browser assessment ' + suffix, { exact: false }),
  ).toBeVisible();
  await expect(teacher.getByText('What is 0.1 plus 0.2?', { exact: false })).toBeVisible();
  await expect(teacher.getByText('Topic: Browser topic ' + suffix, { exact: true })).toHaveCount(2);
  // A draft is the teacher's alone until it is finalized.
  await student.getByRole('link', { name: 'My results', exact: true }).click();
  await student.getByRole('button', { name: 'Refresh results' }).click();
  await expect(student.getByText(/No finalized results yet/)).toBeVisible();
  // The written response waits for the teacher, who marks it with the rubric and then finalizes.
  await expect(teacher.getByText('1 to mark', { exact: true })).toBeVisible();
  await teacher.getByRole('button', { name: 'Review grade', exact: true }).click();
  await expect(
    teacher.getByText('Neither number has an exact binary form, so both are rounded.'),
  ).toBeVisible();
  await teacher.getByRole('button', { name: 'Finalize this grade', exact: true }).click();
  await expect(teacher.getByText('Check the highlighted fields before continuing.')).toBeVisible();
  await teacher.getByRole('radio', { name: /Explains rounding/ }).check();
  await teacher.getByLabel('Feedback on question 2 (optional)').fill('Clear reasoning.');
  await teacher.screenshot({ path: 'test-results/marking-desktop.png', fullPage: true });
  await teacher.getByRole('button', { name: 'Finalize this grade', exact: true }).click();
  await expect(teacher.getByRole('button', { name: 'Cancel', exact: true })).toBeFocused();
  await teacher.getByLabel('Review notes').fill('Reviewed exact decimal answer.');
  await teacher.getByRole('button', { name: 'Finalize grade', exact: true }).click();
  await expect(
    teacher.getByText('Grade finalized. Performance will update after event processing.', { exact: true }),
  ).toBeVisible();
  await student.getByRole('button', { name: 'Refresh results' }).click();
  await expect(
    student.getByRole('heading', { name: 'Browser assessment ' + suffix, exact: true }),
  ).toBeVisible();
  await expect(student.getByText('Correct', { exact: true })).toHaveCount(2);
  await expect(student.getByText('Clear reasoning.', { exact: true })).toBeVisible();
  await expect(student.getByText('Topic: Browser topic ' + suffix, { exact: true })).toHaveCount(2);
  await student.getByRole('link', { name: 'My progress', exact: true }).click();
  await expect(async () => {
    await student.getByRole('button', { name: 'View my performance' }).click();
    await expect(student.getByRole('cell', { name: '100%' })).toBeVisible();
  }).toPass({ timeout: 30000 });
  await expect(student.getByRole('cell', { name: 'Insufficient evidence' })).toBeVisible();
  await expect(
    student.getByRole('rowheader', { name: 'Browser topic ' + suffix, exact: true }),
  ).toBeVisible();
  // The class dashboard counts the finalized grade and shows the class's topics; a student's name opens their
  // full performance.
  await teacher.getByRole('link', { name: 'Class dashboard', exact: true }).click();
  const topics = teacher.getByRole('region', { name: 'Class topics table' });
  await expect(async () => {
    await teacher.getByRole('button', { name: 'Refresh dashboard' }).click();
    await expect(
      topics.getByRole('rowheader', { name: 'Browser topic ' + suffix, exact: true }),
    ).toBeVisible();
  }).toPass({ timeout: 30000 });
  await expect(teacher.getByText('1 of 1 students submitted', { exact: true })).toBeVisible();
  await expect(topics.getByText('100%', { exact: true })).toBeVisible();
  await topics.getByRole('link', { name: 'Student Local', exact: true }).click();
  await expect(teacher).toHaveURL(/\/performance\?student=/);
  await expect(teacher.getByLabel('Student', { exact: true })).toHaveValue(/.+/);
  await expect(teacher.getByRole('cell', { name: '100%' })).toBeVisible();
  await student.screenshot({ path: 'test-results/performance-desktop.png', fullPage: true });
  await expectNoHorizontalScroll(student, 'test-results/performance-mobile.png');
  // The editor and the grading review fit a phone screen.
  await teacherMenu.getByRole('link', { name: 'Assessments', exact: true }).click();
  await expect(teacher.getByRole('heading', { name: 'Create an assessment' })).toBeVisible();
  await expectNoHorizontalScroll(teacher, 'test-results/assessments-mobile.png');
  await teacherMenu.getByRole('link', { name: 'Grading review', exact: true }).click();
  await expectNoHorizontalScroll(teacher, 'test-results/review-mobile.png');
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
  await expect(
    page.getByText(/AI interpretation of student\s+performance is not implemented yet/),
  ).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to workspace' })).toBeFocused();
  await page.screenshot({ path: 'test-results/welcome.png', fullPage: true });
});
