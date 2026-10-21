import { test, expect, Page } from '@playwright/test';
import {
  deleteTestDataAfterEach,
  env,
  expectNoHorizontalScroll,
  login,
  teacherWithClassroom,
} from './helpers';
const CHUNK = '11111111-1111-4111-8111-111111111111',
  FIGURE = '22222222-2222-4222-8222-222222222222',
  DOC = '33333333-3333-4333-8333-333333333333';
// A 1x1 PNG, served in place of a figure crop.
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
  'base64',
);

deleteTestDataAfterEach();

const sse = (events: [string, unknown][]) =>
  events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('');

test('teacher uploads materials and an enrolled student asks the real assistant', async ({ browser }) => {
  const page = await browser.newPage(),
    student = await browser.newPage();
  await login(student, 'student', env.STUDENT_PASSWORD);
  const studentUsername = await student.locator('.identity-note code').innerText();
  const suffix = Date.now().toString();
  await teacherWithClassroom(page, 'Chat ' + suffix);
  const everyone = page.getByRole('region', { name: 'All students' });
  await everyone.getByLabel('Search students').fill(studentUsername);
  await everyone.getByRole('button', { name: 'Enroll Student Local in Chat ' + suffix }).click();
  await expect(page.getByText('Student Local enrolled.', { exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Teaching materials', exact: true }).click();
  await page.getByLabel('Document title').fill('Notes ' + suffix);
  await page.getByLabel('Educational file').setInputFiles({
    name: 'notes.txt',
    mimeType: 'text/plain',
    buffer: Buffer.from('Photosynthesis turns light into chemical energy.'),
  });
  await page.getByRole('button', { name: 'Upload document', exact: true }).click();
  await expect(page.getByText('Document uploaded. Indexing has started.', { exact: true })).toBeVisible();
  const notes = page.locator('.record').filter({ hasText: 'Notes ' + suffix });
  // Text files are indexed too (ADR 0013); without provider keys the test stack never finishes.
  await expect(notes.locator('.badge').first()).toHaveText(/^Index(ing|ed|ing failed)$/);
  await page.getByLabel('Document title').fill('Slides ' + suffix);
  await expect(page.getByLabel('Document type')).toHaveCount(0);
  await page.getByLabel('Educational file').setInputFiles({
    name: 'slides.pdf',
    mimeType: 'application/pdf',
    buffer: Buffer.from('%PDF-1.4\n%%EOF\n'),
  });
  await page.getByRole('button', { name: 'Upload document', exact: true }).click();
  await expect(page.getByText('Document uploaded. Indexing has started.', { exact: true })).toBeVisible();
  const slides = page.locator('.record').filter({ hasText: 'Slides ' + suffix });
  await expect(slides.locator('.badge').first()).toHaveText(/^Indexing( failed)?$/);

  // Deleting a material asks first, with Cancel focused, and then removes it for everyone.
  await notes.getByRole('button', { name: 'Delete Notes ' + suffix }).click();
  const confirm = page.getByRole('dialog', { name: 'Delete this document?' });
  await expect(confirm.getByRole('button', { name: 'Cancel' })).toBeFocused();
  await confirm.getByRole('button', { name: 'Cancel' }).click();
  await expect(notes).toBeVisible();
  await notes.getByRole('button', { name: 'Delete Notes ' + suffix }).click();
  await confirm.getByRole('button', { name: 'Delete document' }).click();
  await expect(page.getByText('Notes ' + suffix + ' deleted.', { exact: true })).toBeVisible();
  await expect(notes).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Classroom library' })).toBeFocused();

  // The student sees what is left, and asks with their own access.
  await student.getByRole('button', { name: 'Refresh classrooms' }).click();
  await student.getByLabel('Current classroom').selectOption({ label: 'Chat ' + suffix });
  await student.getByRole('link', { name: 'Teaching materials', exact: true }).click();
  await expect(student.locator('.record').filter({ hasText: 'Slides ' + suffix })).toBeVisible();
  await expect(student.getByText('Notes ' + suffix)).toHaveCount(0);
  await expect(student.getByRole('button', { name: /Index document|Retry indexing|Delete/ })).toHaveCount(0);
  await page.close();
  await answerHonestly(student);
  await student.close();
});

async function answerHonestly(page: Page) {
  await page.getByRole('link', { name: 'Ask the materials', exact: true }).click();
  await expect(page.getByText('No conversations yet. Ask a question to start one.')).toBeVisible();
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  await expect(page.getByLabel('Your question')).toHaveAttribute('aria-invalid', 'true');
  await expect(page.getByLabel('Your question')).toBeFocused();
  await page.getByLabel('Your question').fill('What does photosynthesis produce?');
  await page.getByLabel('Your question').press('Control+Enter');
  // With or without provider keys the turn must end honestly: an answer, "no matching material", or a stated failure.
  const answer = page.locator('.turn.answer').last();
  await expect(answer.locator('.cursor')).toHaveCount(0, { timeout: 60000 });
  await expect(page.getByRole('button', { name: 'Send question', exact: true })).toBeEnabled();
  await expect(page.getByLabel('Your question')).toHaveValue('');
  await expect(page.getByRole('button', { name: /What does photosynthesis produce\?/ })).toBeVisible();
  const failed = await page.getByRole('alert').count();
  if (failed) await expect(answer.getByText('Failed', { exact: true })).toBeVisible();
  else await expect(answer.locator('.turn-text')).not.toHaveText('');
}

test('chat streams cited answers, loads figures and reports failures', async ({ page }) => {
  const suffix = Date.now().toString();
  await teacherWithClassroom(page, 'Mocked chat ' + suffix);
  const room = await page.getByLabel('Current classroom').inputValue();
  const thread = {
    id: '44444444-4444-4444-8444-444444444444',
    classroom_id: room,
    role: 'teacher',
    title: null as string | null,
    created_at: new Date().toISOString(),
    last_message_at: new Date().toISOString(),
  };
  const citations = [
    {
      chunk_id: CHUNK,
      document_id: DOC,
      title: 'Biology notes',
      page_start: 3,
      page_end: 3,
      kind: 'text',
      source: 'pdf',
      content: 'Plants make glucose and oxygen.',
      image: null,
    },
    {
      chunk_id: FIGURE,
      document_id: DOC,
      title: 'Biology notes',
      page_start: 4,
      page_end: 5,
      kind: 'figure',
      source: 'vision',
      content: 'A leaf cross-section.',
      image: '0123456789abcdef.png',
    },
  ];
  let saved: unknown[] = [];
  const answerText = `Plants make:\n\n- **Glucose** [doc:${CHUNK}]\n- \`oxygen\`, see the leaf [doc:${FIGURE}].`;
  let reply = sse([
    ['token', { text: 'Plants make:\n\n- **Glu' }],
    ['token', { text: answerText.slice(21) }],
    ['citations', { citations }],
    ['done', { message_id: 'm2', outcome: 'answered' }],
  ]);
  await page.route(
    (url) => url.pathname === '/api/chatbot/threads',
    (route) =>
      route.request().method() === 'POST'
        ? route.fulfill({ status: 201, json: thread })
        : route.fulfill({ json: { threads: thread.title ? [thread] : [] } }),
  );
  await page.route(`**/api/chatbot/threads/${thread.id}/messages`, async (route) => {
    if (route.request().method() === 'GET') return route.fulfill({ json: { messages: saved } });
    const asked = route.request().postDataJSON().content as string;
    thread.title = asked;
    const body = reply;
    saved = [
      {
        id: 'm1',
        role: 'user',
        content: asked,
        outcome: null,
        citations: null,
        created_at: thread.created_at,
      },
      {
        id: 'm2',
        role: 'assistant',
        content: body.includes('error') ? '' : answerText,
        outcome: body.includes('error') ? 'error' : 'answered',
        citations: body.includes('error') ? null : citations,
        created_at: thread.created_at,
      },
    ];
    return route.fulfill({ headers: { 'Content-Type': 'text/event-stream' }, body });
  });
  await page.route(`**/api/content/documents/${DOC}/figures/*`, (route) =>
    route.fulfill({ contentType: 'image/png', body: PNG }),
  );

  await page.getByRole('link', { name: 'Ask the materials', exact: true }).click();
  await page.getByLabel('Your question').fill('What do plants make?');
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'Answer complete.' })).toBeVisible();
  const answer = page.locator('.turn.answer').last();
  await expect(answer.locator('.turn-text p')).toHaveText('Plants make:');
  await expect(answer.locator('.turn-text li')).toHaveText(['Glucose [1]', 'oxygen, see the leaf [2].']);
  await expect(answer.locator('.turn-text strong')).toHaveText('Glucose');
  await expect(answer.locator('.turn-text code')).toHaveText('oxygen');
  await expect(answer.getByText('**')).toHaveCount(0);
  await expect(page.getByRole('button', { name: /What do plants make\?/ })).toHaveAttribute(
    'aria-current',
    'true',
  );
  await answer.getByRole('link', { name: 'Source 2' }).click();
  const figure = answer.locator('.sources li').nth(1);
  await expect(figure).toBeFocused();
  await expect(figure.getByText('This passage was written by a model from the page image.')).toBeVisible();
  await expect(figure.getByRole('img', { name: 'Figure from Biology notes, pages 4–5' })).toBeVisible();
  await answer.locator('.sources summary').first().focus();
  await page.keyboard.press('Enter');
  await expect(answer.getByText('Plants make glucose and oxygen.')).toBeVisible();
  await expect(answer.getByRole('button', { name: 'Open the PDF at page 3' })).toBeVisible();
  await answer.screenshot({ path: 'test-results/chat-answer.png' });

  reply = sse([['error', { code: 'AI_UNAVAILABLE', message: 'The assistant is not available right now.' }]]);
  await page.getByLabel('Your question').fill('And then?');
  await page.getByLabel('Your question').press('Control+Enter');
  await expect(
    page.getByRole('alert').filter({ hasText: 'The assistant is not available right now.' }),
  ).toBeVisible();
  await expect(page.locator('.turn.answer').last().getByText('Failed', { exact: true })).toBeVisible();

  await page.route(`**/api/chatbot/threads/${thread.id}/messages`, (route) =>
    route.fulfill({
      status: 503,
      json: { error: { code: 'UNAVAILABLE', message: 'The chatbot is restarting.' } },
    }),
  );
  await page.getByLabel('Your question').fill('Kept draft');
  await page.getByRole('button', { name: 'Send question', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: 'The chatbot is restarting.' })).toBeVisible();
  await expect(page.getByLabel('Your question')).toHaveValue('Kept draft');
  await expect(page.getByRole('status').filter({ hasText: 'Answering…' })).toHaveCount(0);
  await expect(page.getByLabel('Current classroom')).toBeDisabled();
  await page.screenshot({ path: 'test-results/chat-desktop.png', fullPage: true });
  await expectNoHorizontalScroll(page, 'test-results/chat-mobile.png');
});
