import { test, expect, type Route } from '@playwright/test';
import {
  archiveTopic,
  deleteTestDataAfterEach,
  expectNoHorizontalScroll,
  restoreTopic,
  topicToDelete,
  teacherWithClassroom,
} from './helpers';

deleteTestDataAfterEach();

// The agent and the classroom's documents are stubbed in the browser: real generation needs provider keys and
// indexed PDFs. The editor, the curriculum and saving the draft are real.
const SLIDES = '00000000-0000-4000-8000-000000000001';
const SCHEME = '00000000-0000-4000-8000-000000000002';
const doc = (id: string, title: string, docType: string) => ({
  id,
  classroom_id: 'c',
  title,
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  doc_type: docType,
  page_count: 3,
  chunk_count: 4,
});
const source = { document_id: SLIDES, chunk_id: SLIDES, page: 2, text: 'A unit test checks one function.' };

function draft(topicId: string, prompt: string) {
  return {
    question: {
      prompt,
      type: 'multiple_choice',
      options: ['A unit test', 'An end-to-end test'],
      answer: 'A unit test',
      points: 1,
      topic_id: topicId,
      sources: [source],
      origin: 'generated',
    },
    evidence: 'A unit test checks one function.',
    explanation: 'The slides define a unit test as checking one function.',
  };
}

const json = (route: Route, status: number, body: unknown) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

test('a teacher generates questions and drafts marking guidance', async ({ page }) => {
  const suffix = Date.now().toString();
  await teacherWithClassroom(page, 'Generation ' + suffix);
  await page.getByRole('link', { name: 'Curriculum', exact: true }).click();
  await page.getByLabel('Name', { exact: true }).fill(topicToDelete('Testing topic ' + suffix));
  await page.getByRole('button', { name: 'Add topic', exact: true }).click();
  await expect(page.getByText('Topic added.', { exact: true })).toBeVisible();

  await page.route(/\/api\/content\/documents\?classroom_id=/, (route) =>
    json(route, 200, [
      doc(SLIDES, 'Testing slides', 'teaching_material'),
      doc(SCHEME, 'Testing scheme', 'marking_scheme'),
    ]),
  );
  let generations = 0;
  await page.route('**/api/question-generation/questions', (route) => {
    const body = route.request().postDataJSON();
    if (generations++ === 0)
      return json(route, 503, {
        error: {
          code: 'SEARCH_UNAVAILABLE',
          message: 'Searching the class materials failed. Try again later.',
        },
      });
    return json(route, 200, {
      questions: [
        draft(body.topic_id, 'Which test checks one function?'),
        draft(body.topic_id, 'Which test is fastest?'),
      ],
      requested: 3,
      returned: 2,
      dropped: { DUPLICATE: 1 },
      requires_teacher_review: true,
    });
  });
  await page.route('**/api/question-generation/marking-guidance', (route) =>
    json(route, 200, {
      type: 'short_answer',
      accepted_answers: ['unit test', 'a unit test'],
      rubric: null,
      marking_points: { text: '1 mark: names a unit test.' },
      sources: [source],
      evidence: 'A unit test checks one function.',
      requires_teacher_review: true,
    }),
  );
  await page.route('**/api/chatbot/usage', (route) =>
    json(route, 200, { spent_usd: 0.01, limit_usd: 10, resets_at: new Date().toISOString() }),
  );

  await page.getByRole('link', { name: 'Assessments', exact: true }).click();
  await page.getByRole('button', { name: 'Refresh assessments' }).click();
  await page.getByLabel('Assessment title').fill('Generated ' + suffix);
  await page.getByText('Generate questions from teaching materials').click();
  await page.getByRole('checkbox', { name: 'Testing slides' }).check();
  // A marking scheme uploaded before they were retired is not a source for questions.
  await expect(page.getByRole('checkbox', { name: 'Testing scheme' })).toHaveCount(0);
  await page.locator('#generate-topic').selectOption({ label: 'Testing topic ' + suffix });
  await page.getByRole('button', { name: 'Generate questions' }).click();
  await expect(page.getByText('Searching the class materials failed. Try again later.')).toBeVisible();
  // Enter in the focus field generates instead of saving the assessment.
  await page.getByLabel('Focus (optional)').fill('unit tests');
  await page.getByLabel('Focus (optional)').press('Enter');
  await expect(page.getByText(/2 of 3\s+questions drafted\.\s+1 draft was left out/)).toBeVisible();
  const firstDraft = page.getByRole('group', { name: 'Generated question 1' });
  await expect(firstDraft.getByText('AI-written draft')).toBeVisible();
  await firstDraft.getByRole('button', { name: 'Add to assessment generated question 1' }).click();
  await expect(page.getByText('Generated question added as question 1.')).toBeVisible();
  await page.getByRole('button', { name: 'Discard generated question 1' }).click();
  await expect(page.getByRole('group', { name: /^Generated question/ })).toHaveCount(0);

  const first = page.getByRole('group', { name: 'Question 1', exact: true });
  await expect(first.getByLabel('Question', { exact: true })).toHaveValue('Which test checks one function?');
  await first.getByText('AI-generated from 1 passage of your materials').click();
  await expect(first.getByText('Testing slides, page 2')).toBeVisible();

  // A hand-written short answer gets drafted guidance, from teaching materials only, then a marking point of the
  // teacher's own.
  await page.getByRole('button', { name: 'Add question', exact: true }).click();
  const second = page.getByRole('group', { name: 'Question 2', exact: true });
  await second.getByLabel('Question', { exact: true }).fill('Name the test that checks one function.');
  await second.getByLabel('Question type').selectOption('short_answer');
  await expect(second.getByLabel('Draft from').locator('option')).toHaveText([
    'All teaching materials',
    'Testing slides',
  ]);
  await second.getByRole('button', { name: 'Draft marking guidance for question 2' }).click();
  await expect(second.getByText('AI-written guidance')).toBeVisible();
  await expect(second.getByLabel('Accepted answers')).toHaveValue('');
  await second.getByRole('button', { name: 'Use this guidance for question 2' }).click();
  await expect(second.getByLabel('Accepted answers')).toHaveValue('unit test\na unit test');
  const points = second.getByRole('group', { name: 'Marking points (optional)' });
  await expect(points.getByText('1 mark: names a unit test.')).toBeVisible();
  await expect(points.getByText('Written by AI', { exact: true })).toBeVisible();
  await points.getByLabel('Add a marking point').fill('Accept "unit testing".');
  await points.getByRole('button', { name: 'Add marking point to question 2' }).click();
  await expect(points.getByText('Written by you', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Match scheme to questions' })).toHaveCount(0);

  await page.getByRole('button', { name: 'Create draft' }).click();
  await expect(page.getByText('Draft created.', { exact: true })).toBeVisible();
  await expectNoHorizontalScroll(page, 'test-results/generation-mobile.png');
  await page.setViewportSize({ width: 1280, height: 800 });
  await archiveTopic(page, 'Testing topic ' + suffix);
  await restoreTopic(page, 'Testing topic ' + suffix);
});
