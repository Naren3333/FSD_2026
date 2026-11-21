import { test, expect, type Route } from '@playwright/test';
import { deleteTestDataAfterEach, expectNoHorizontalScroll, openMenu, teacherWithClassroom } from './helpers';

deleteTestDataAfterEach();

// the agent is stubbed in the browser: a real answer needs provider keys and released results.
const ANA = '00000000-0000-4000-8000-0000000000a1';
const TOPIC = '00000000-0000-4000-8000-0000000000b1';

const json = (route: Route, status: number, body: unknown) =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

const answer = (question: string) => ({
  classroom_id: 'c',
  question,
  struggling: [{ topic_id: TOPIC, student_ids: [ANA], text: 'Ana lost most marks on recursion.' }],
  observations: [
    { text: 'Base cases are often missing.', topic_ids: [TOPIC], student_ids: [ANA], evidence_ids: ['E1'] },
  ],
  possible_misconceptions: [{ text: 'Recursion never stops on its own.', evidence_ids: ['E1'] }],
  reteach: [{ topic_id: TOPIC, text: 'Trace a recursive call on the board.', evidence_ids: ['E1'] }],
  students: [{ student_id: ANA, display_name: 'Ana Lim' }],
  evidence: [
    {
      evidence_id: 'E1',
      student_id: ANA,
      question_id: 'q1',
      evaluation_id: 'e1',
      assessment_id: 'a1',
      assessment_title: 'Recursion quiz',
      topic_id: TOPIC,
      prompt: 'What does fact(3) return?',
      awarded: 0,
      possible: 2,
    },
  ],
  steps: [{ tool: 'topic_questions', reason: 'Recursion looks weak.', target: 'Recursion', outcome: 'ok' }],
  model_turns: 2,
  dropped: { NOT_SUPPORTED: 1 },
  scope: { population: 250, compared: 200, partial: true, selection: 'roster_order' },
  requires_teacher_review: true,
});

test('a teacher asks about the class, follows up and starts again', async ({ page }) => {
  await teacherWithClassroom(page, 'Insights ' + Date.now());
  const requests: { question: string; previous_questions?: string[] }[] = [];
  await page.route('**/api/performance-agent/class_insights', (route) => {
    const body = route.request().postDataJSON();
    requests.push(body);
    if (requests.length === 1)
      return json(route, 422, {
        error: {
          code: 'NO_EVIDENCE',
          message: 'This classroom has no released results yet, so there is nothing to answer from.',
        },
      });
    if (requests.length === 2)
      return json(route, 429, {
        error: {
          code: 'AI_SPEND_LIMIT',
          message: "You've used today's AI allowance. It resets at 12:00 am.",
        },
      });
    return json(route, 200, answer(body.question));
  });
  await page.route('**/api/chatbot/usage', (route) =>
    json(route, 200, { spent_usd: 0.01, limit_usd: 10, resets_at: new Date().toISOString() }),
  );

  await openMenu(page);
  await page.getByRole('link', { name: 'Class dashboard', exact: true }).click();
  const panel = page.getByRole('region', { name: 'Ask about this class' });
  await expect(panel.getByText('AI draft for your review')).toBeVisible();
  const field = panel.getByLabel('Your question');

  // a suggestion fills the question; a class with nothing released is an empty state, not an error.
  await panel.getByRole('button', { name: 'Who is struggling, and with which topics?' }).click();
  await expect(field).toHaveValue('Who is struggling, and with which topics?');
  await expect(field).toBeFocused();
  await panel.getByRole('button', { name: 'Ask', exact: true }).click();
  await expect(panel.getByText('This classroom has no released results yet')).toBeVisible();
  await expect(panel.getByRole('alert')).toHaveCount(0);
  await expect(field).toHaveValue('Who is struggling, and with which topics?');

  // a refusal shows why, keeps the question and is not retried.
  await field.press('Control+Enter');
  await expect(panel.getByRole('alert')).toHaveText(
    "You've used today's AI allowance. It resets at 12:00 am.",
  );
  await expect(field).toHaveValue('Who is struggling, and with which topics?');
  expect(requests).toHaveLength(2);

  await panel.getByRole('button', { name: 'Ask', exact: true }).click();
  await expect(panel.locator('#class-insights-answer')).toBeFocused();
  await expect(panel.getByText('Covers the first 200 of 250 students in roster order.')).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'Struggling' })).toBeVisible();
  await expect(panel.getByRole('heading', { name: 'What to reteach' })).toBeVisible();
  await expect(
    panel.getByText('Ana Lim · Recursion quiz · What does fact(3) return? · 0 of 2 marks').first(),
  ).toBeVisible();
  await expect(panel.getByRole('link', { name: 'Ana Lim' }).first()).toHaveAttribute(
    'href',
    `/performance?student=${ANA}`,
  );
  await expect(panel.getByText('1 statement was left out')).toBeVisible();
  await panel.getByText('How the agent worked (1 step, 2 model turns)').click();
  await expect(panel.getByText('Read the topic’s questions: Recursion (done).')).toBeVisible();

  // a follow-up carries the earlier question; New question starts over.
  await panel.getByLabel('Your follow-up question').fill('And what should I reteach first?');
  await panel.getByRole('button', { name: 'Ask a follow-up' }).click();
  await expect(panel.getByText('You asked: And what should I reteach first?')).toBeVisible();
  expect(requests[3].previous_questions).toEqual(['Who is struggling, and with which topics?']);
  await panel.getByRole('button', { name: 'New question' }).click();
  await expect(panel.getByText('You asked:')).toHaveCount(0);
  await panel.getByLabel('Your question').fill('Who needs help?');
  await panel.getByRole('button', { name: 'Ask', exact: true }).click();
  await expect(panel.getByText('You asked: Who needs help?')).toBeVisible();
  expect(requests[4].previous_questions).toEqual([]);

  await expectNoHorizontalScroll(page, 'test-results/class-insights-narrow.png');
});
