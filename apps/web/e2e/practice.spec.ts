import { test, expect, type Route } from '@playwright/test';
import {
  deleteTestDataAfterEach,
  enrollStudent,
  env,
  expectNoHorizontalScroll,
  login,
  teacherWithClassroom,
} from './helpers';

deleteTestDataAfterEach();

// the agent and the classroom's documents are stubbed in the browser: real practice needs provider keys
// and indexed pdfs.
const SLIDES = '00000000-0000-4000-8000-000000000001';
const SCHEME = '00000000-0000-4000-8000-000000000002';
const doc = (id: string, title: string, docType: string) => ({
  id,
  classroom_id: 'c',
  title,
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  sha256: 'a'.repeat(64),
  doc_type: docType,
  page_count: 3,
  chunk_count: 4,
});
const source = { document_id: SLIDES, chunk_id: SLIDES, page: 2, text: 'A unit test checks one function.' };
const choice = {
  prompt: 'Which test checks one function?',
  type: 'multiple_choice',
  options: ['A unit test', 'An end-to-end test'],
  answer: 'A unit test',
  accepted_answers: [],
  explanation: 'The slides say a unit test checks one function.',
  evidence: 'A unit test checks one function.',
  sources: [source],
  topic_id: null,
};
const short = {
  ...choice,
  prompt: 'What do we call a test of one function?',
  type: 'short_answer',
  options: [],
  answer: '',
  accepted_answers: ['unit test', 'unit testing'],
};

const json = (route: Route, status: number, body: unknown) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('an enrolled student practises from chosen materials, with each key shown only after a check', async ({
  browser,
}) => {
  const student = await browser.newPage();
  await login(student, 'student', env.STUDENT_PASSWORD);
  const studentUsername = await student.locator('.identity-note code').innerText();
  const room = 'Practice ' + Date.now().toString();
  const teacher = await browser.newPage();
  await teacherWithClassroom(teacher, room);
  await enrollStudent(teacher, studentUsername, 'Student Local', room);
  await teacher.close();

  await student.route(/\/api\/content\/documents\/page\?classroom_id=/, (route) =>
    json(route, 200, {
      items: [
        doc(SLIDES, 'Testing slides', 'teaching_material'),
        doc(SCHEME, 'Old scheme', 'marking_scheme'),
      ],
      total: 2,
    }),
  );
  const replies = [
    () => ({
      status: 429,
      body: {
        error: {
          code: 'AI_SPEND_LIMIT',
          message: "You've used today's AI allowance. It resets at 00:00 on 23 Nov (UTC+08:00).",
        },
      },
    }),
    () => ({
      status: 422,
      body: {
        error: {
          code: 'GENERATION_REJECTED',
          message:
            'None of the practice questions passed the checks. Try other materials or a different focus.',
        },
      },
    }),
    () => ({
      status: 200,
      body: {
        questions: [choice, short],
        requested: 4,
        returned: 2,
        dropped: { WEAK_DISTRACTORS: 3 },
        model_calls: 2,
      },
    }),
  ];
  const requests: { document_ids: string[]; counts: Record<string, number>; topic_id: string | null }[] = [];
  await student.route('**/api/question-generation/practice', (route) => {
    requests.push(route.request().postDataJSON());
    const reply = replies[Math.min(requests.length - 1, replies.length - 1)]();
    return json(route, reply.status, reply.body);
  });
  await student.route('**/api/chatbot/usage', (route) =>
    json(route, 200, { spent_usd: 0.01, limit_usd: 5, resets_at: new Date().toISOString() }),
  );

  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await student.getByLabel('Current classroom').selectOption({ label: room });
  await student
    .getByRole('navigation', { name: 'Workspace' })
    .getByRole('link', { name: 'Practice', exact: true })
    .click();
  await expect(student.getByRole('heading', { name: 'Practice', level: 2 })).toBeVisible();
  await expect(student.getByText('Each set uses your daily AI allowance', { exact: false })).toBeVisible();
  // a marking scheme uploaded before they were retired is not something to practise from.
  await expect(student.getByRole('checkbox', { name: 'Old scheme' })).toHaveCount(0);

  // nothing chosen: said in place, with no request.
  await student.getByRole('button', { name: 'Generate practice' }).click();
  await expect(student.getByText('Choose at least one teaching material.')).toBeVisible();
  expect(requests).toHaveLength(0);

  await student.getByRole('checkbox', { name: 'Testing slides' }).check();
  await student.getByLabel('Short answer', { exact: true }).fill('1');
  await student.getByRole('button', { name: 'Generate practice' }).click();
  // the spend limit says when it resets, keeps the choices and is not retried.
  await expect(student.getByText('It resets at 00:00 on 23 Nov (UTC+08:00).')).toBeVisible();
  await expect(student.getByRole('checkbox', { name: 'Testing slides' })).toBeChecked();
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({
    document_ids: [SLIDES],
    counts: { multiple_choice: 3, short_answer: 1 },
    topic_id: null,
  });
  await student.getByRole('button', { name: 'Generate practice' }).click();
  await expect(
    student.getByText('None of the practice questions passed the checks.', { exact: false }),
  ).toBeVisible();
  await student.getByRole('button', { name: 'Generate practice' }).click();
  await expect(student.getByText('2 practice questions ready.')).toBeVisible();
  await expect(
    student.getByText(
      '2 of the 4 questions you asked for were left out because the checks could not confirm them.',
    ),
  ).toBeVisible();
  await expect(student.getByText('AI-written practice')).toHaveCount(2);
  await expect(student.locator('#practice-0-prompt')).toBeFocused();

  // no key, explanation or passage until the student checks an answer.
  await expect(student.getByText('The slides say a unit test checks one function.')).toHaveCount(0);
  await expect(student.getByText('Accepted answers', { exact: false })).toHaveCount(0);
  await expect(student.getByText('Testing slides, page 2')).toHaveCount(0);

  // keyboard only: arrow keys choose an option in the radio group, then tab reaches its check.
  await student.locator('#practice-0-answer').focus();
  await student.keyboard.press('ArrowDown');
  await expect(student.getByRole('radio', { name: 'An end-to-end test' })).toBeChecked();
  await student.keyboard.press('Tab');
  await expect(student.locator('#practice-0-check')).toBeFocused();
  await student.keyboard.press('Enter');
  const feedback = student.getByRole('group', { name: 'Feedback on question 1' });
  await expect(feedback).toBeFocused();
  await expect(feedback.getByText('Incorrect', { exact: true })).toBeVisible();
  await expect(feedback.getByText('The answer is: A unit test', { exact: false })).toBeVisible();
  await expect(feedback.getByText('Why: The slides say a unit test checks one function.')).toBeVisible();
  await expect(student.getByRole('radio', { name: 'A unit test' })).toBeDisabled();

  // a short answer is checked with enter; punctuation and case do not matter.
  await student.locator('#practice-1-answer').focus();
  await student.keyboard.type('Unit-Test.');
  await student.keyboard.press('Enter');
  const second = student.getByRole('group', { name: 'Feedback on question 2' });
  await expect(second).toBeFocused();
  await expect(second.getByText('Correct', { exact: true })).toBeVisible();
  await expect(second.getByText('Accepted answers: unit test; unit testing')).toBeVisible();
  await expect(student.getByText('1 of 2 correct.')).toBeVisible();
  await second.getByText('From 1 passage of your materials').click();
  await expect(second.getByText('Testing slides, page 2')).toBeVisible();

  await expectNoHorizontalScroll(student, 'test-results/practice-mobile.png');
  await student.close();
});
