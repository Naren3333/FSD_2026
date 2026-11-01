// @vitest-environment jsdom
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { Router, provideRouter } from '@angular/router';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { AssessmentApi } from '../../api/clients';
import type { BankFilter, BankQuestion, BankQuestionInput, Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ApiError } from '../../core/gateway';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent } from '../../shared/field';
import { PageHeaderComponent } from '../../shared/page-header';
import { PagerComponent } from '../../shared/pager';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { QuestionBankComponent } from './bank';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => {
  TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting());
  // jsdom has no modal dialogs: these do what a browser's do for the parts the dialog relies on.
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    if (!this.open) return;
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
});

@Component({ standalone: true, template: '' })
class StubPage {}

const question = (change: Partial<BankQuestion> = {}): BankQuestion => ({
  id: 'b1',
  prompt: 'Half of 1?',
  type: 'numerical',
  options: [],
  answer: '0.5',
  tolerance: '0',
  points: 2,
  topic_id: 't1',
  created_at: '2026-11-01T00:00:00Z',
  updated_at: '2026-11-01T00:00:00Z',
  ...change,
});
const topic = (id: string, name: string): Topic => ({ id, name }) as Topic;

/* the bank page against a fake bank holding `bank`; `api` replaces any of its calls. */
function render(bank: BankQuestion[] | Promise<never>, api: Record<string, unknown> = {}) {
  const held = { items: bank };
  const fake = {
    bankPage: vi.fn(async (_filter: BankFilter, offset: number, limit: number) => {
      const items = await held.items;
      return { items: items.slice(offset, offset + limit), total: items.length };
    }),
    bankQuestion: vi.fn((id: string) => Promise.resolve(question({ id }))),
    addToBank: vi.fn((q: BankQuestionInput) => Promise.resolve(question({ ...q, id: 'b9' }))),
    updateBankQuestion: vi.fn((id: string, q: BankQuestionInput) => Promise.resolve(question({ ...q, id }))),
    removeBankQuestion: vi.fn(() => Promise.resolve()),
    ...api,
  };
  const archived = signal<Topic[]>([topic('t9', 'Old units')]);
  const names: Record<string, string> = { t1: 'Fractions', t2: 'Decimals', t9: 'Old units' };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        { path: '', component: StubPage },
        { path: 'question-bank', component: StubPage },
      ]),
      { provide: AssessmentApi, useValue: fake },
      {
        provide: CurriculumNames,
        useValue: {
          topics: signal([topic('t1', 'Fractions'), topic('t2', 'Decimals')]),
          archived,
          topicName: (id: string) => names[id] ?? '',
          refresh: vi.fn(() => Promise.resolve([])),
        },
      },
    ],
  });
  TestBed.overrideComponent(QuestionBankComponent, { set: { template: template('./bank.html') } });
  for (const [component, path] of [
    [PageHeaderComponent, '../../shared/page-header.html'],
    [PagerComponent, '../../shared/pager.html'],
    [SectionStateComponent, '../../shared/section-state.html'],
    [FieldComponent, '../../shared/field.html'],
    [StatusComponent, '../../shared/status.html'],
    [ConfirmDialogComponent, '../../shared/confirm-dialog.html'],
  ] as const)
    TestBed.overrideComponent(component, { set: { template: template(path) } });
  const fixture = TestBed.createComponent(QuestionBankComponent);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const page = fixture.componentInstance;
  const router = TestBed.inject(Router);
  const settle = async () => {
    for (let i = 0; i < 3; i++) await new Promise((resolve) => setTimeout(resolve));
    fixture.detectChanges();
  };
  const list = () => root.querySelector('[aria-labelledby="bank-title"]')?.textContent ?? '';
  const rows = () =>
    [...root.querySelectorAll('tr.record')].map((r) =>
      [...r.querySelectorAll('th, td[data-label]')].map((c) => c.textContent?.replace(/\s+/g, ' ').trim()),
    );
  const formShown = () => !!root.querySelector('#bank-form') && !root.querySelector('.bank-edit[hidden]');
  const input = <T extends HTMLElement>(id: string) => root.querySelector<T>('#' + id)!;
  /* types into a control as a user would. */
  const type = (id: string, value: string) => {
    const el = input<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(id);
    el.value = value;
    el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input'));
    fixture.detectChanges();
  };
  const go = async (url: string) => {
    await router.navigateByUrl(url);
    await settle();
  };
  return { root, page, fake, held, archived, settle, list, rows, formShown, input, type, go, fixture };
}

describe('the bank list', () => {
  it('shows loading, not the empty text, until the bank arrives', async () => {
    const view = render(new Promise<never>(() => undefined));
    await view.settle();
    expect(view.list()).toContain('Loading your bank…');
    expect(view.list()).not.toContain('Your bank is empty');
  });

  it('says how to fill an empty bank', async () => {
    const view = render([]);
    await view.settle();
    expect(view.list()).toContain('Choose Save to bank on a question of an assessment you have saved');
    expect(view.list()).toContain('New question');
  });

  it('shows a failure in place of the list, and Refresh reads it again', async () => {
    const view = render(Promise.reject(new Error('Assessment is unavailable.')));
    await view.settle();
    expect(view.list()).toContain('Assessment is unavailable.');
    expect(view.list()).not.toContain('Your bank is empty');
    view.held.items = [question()];
    view.root.querySelector<HTMLButtonElement>('[aria-label="Refresh question bank"]')?.click();
    await view.settle();
    expect(view.rows()).toEqual([['Half of 1?', 'Numerical', '2 marks', 'Fractions']]);
  });

  it('shows each question’s prompt, type, marks and topic, marking an archived topic', async () => {
    const view = render([
      question(),
      question({ id: 'b2', prompt: 'Pick one', type: 'multiple_choice', points: 1, topic_id: 't9' }),
    ]);
    await view.settle();
    expect(view.rows()[0]).toEqual(['Half of 1?', 'Numerical', '2 marks', 'Fractions']);
    expect(view.rows()[1].slice(0, 3)).toEqual(['Pick one', 'Multiple choice', '1 mark']);
    expect(view.rows()[1][3]).toContain('Old units (archived)');
    expect(view.rows()[1][3]).toContain('cannot be added to an assessment');
  });

  it('searches with the filters from the first page, and says when nothing matches', async () => {
    const view = render([question()]);
    await view.settle();
    view.page.questions.page.set(2);
    view.type('bank-filter-text', 'half');
    view.type('bank-filter-topic', 't1');
    view.type('bank-filter-type', 'numerical');
    view.held.items = [];
    view.root.querySelector<HTMLFormElement>('#bank-filters')?.dispatchEvent(new Event('submit'));
    await view.settle();
    expect(view.fake.bankPage).toHaveBeenLastCalledWith(
      { topicId: 't1', type: 'numerical', text: 'half' },
      0,
      20,
    );
    expect(view.list()).toContain('No bank questions match. Clear the filters to see all of them.');
    view.held.items = [question()];
    [...view.root.querySelectorAll('button')].find((b) => b.textContent?.includes('Clear filters'))?.click();
    await view.settle();
    expect(view.fake.bankPage).toHaveBeenLastCalledWith({ topicId: '', type: undefined, text: '' }, 0, 20);
    expect(view.rows()).toHaveLength(1);
  });

  it('pages twenty at a time', async () => {
    const view = render(Array.from({ length: 25 }, (_, i) => question({ id: `b${i}`, prompt: `Q${i}` })));
    await view.settle();
    expect(view.rows()).toHaveLength(20);
    [...view.root.querySelectorAll('button')].find((b) => b.textContent?.includes('Next'))?.click();
    await view.settle();
    expect(view.fake.bankPage).toHaveBeenLastCalledWith(expect.anything(), 20, 20);
    expect(view.rows().map((r) => r[0])).toEqual(['Q20', 'Q21', 'Q22', 'Q23', 'Q24']);
  });
});

describe('creating and editing a bank question', () => {
  it('adds a new question, announces it and re-reads the list', async () => {
    const view = render([]);
    await view.settle();
    view.input<HTMLButtonElement>('new-question').click();
    await view.settle();
    expect(view.formShown()).toBe(true);
    expect(view.root.textContent).toContain('New bank question');
    // no marking points or passages in the bank.
    expect(view.root.querySelector('#bank-form')?.textContent).not.toContain('Marking point');
    view.type('bank-question-prompt', 'A third of 3?');
    view.type('bank-question-key', '1');
    view.type('bank-question-topic', 't2');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(true);
    view.held.items = [question({ id: 'b9', prompt: 'A third of 3?' })];
    view.input<HTMLFormElement>('bank-form').dispatchEvent(new Event('submit'));
    await view.settle();
    const sent = view.fake.addToBank.mock.calls[0][0];
    expect(sent).toMatchObject({ prompt: 'A third of 3?', type: 'numerical', answer: '1', topic_id: 't2' });
    for (const key of ['marking_scheme', 'sources', 'bank_question_id']) expect(sent).not.toHaveProperty(key);
    expect(view.page.state.message()).toBe('Question added to your bank.');
    expect(view.page.view()).toBe('list');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(false);
    expect(view.rows()[0][0]).toBe('A third of 3?');
  });

  it('keeps the input and focuses the first invalid control', async () => {
    const view = render([]);
    await view.settle();
    await view.go('/?edit=new');
    view.type('bank-question-prompt', 'Unfinished');
    view.input<HTMLFormElement>('bank-form').dispatchEvent(new Event('submit'));
    await view.settle();
    expect(view.fake.addToBank).not.toHaveBeenCalled();
    expect(view.page.state.error()).toBe('Check the highlighted fields before continuing.');
    expect(document.activeElement?.id).toBe('bank-question-topic');
    expect(view.input<HTMLTextAreaElement>('bank-question-prompt').value).toBe('Unfinished');
  });

  it('edits a listed question in place and saves it', async () => {
    const view = render([question()]);
    await view.settle();
    view.input<HTMLButtonElement>('edit-b1').click();
    await view.settle();
    expect(view.root.textContent).toContain('Edit bank question');
    expect(view.input<HTMLTextAreaElement>('bank-question-prompt').value).toBe('Half of 1?');
    // the list held it, so it is not read again.
    expect(view.fake.bankQuestion).not.toHaveBeenCalled();
    view.type('bank-question-points', '3');
    view.input<HTMLFormElement>('bank-form').dispatchEvent(new Event('submit'));
    await view.settle();
    expect(view.fake.updateBankQuestion).toHaveBeenCalledWith('b1', expect.objectContaining({ points: 3 }));
    expect(view.page.state.message()).toBe('Bank question saved.');
    expect(view.page.view()).toBe('list');
  });

  it('reads a question the address names that the list does not hold, and says when it is gone', async () => {
    const view = render([], {
      bankQuestion: vi.fn(() => Promise.reject(new ApiError('not found', 404, 'NOT_FOUND'))),
    });
    await view.settle();
    await view.go('/?edit=b5');
    expect(view.fake.bankQuestion).toHaveBeenCalledWith('b5');
    expect(view.root.textContent).toContain('This bank question no longer exists. It may have been deleted.');
    expect(view.root.querySelector('#bank-form')).toBeNull();
  });

  it('asks for an active topic before saving a question whose topic was archived', async () => {
    const view = render([question({ topic_id: 't9' })]);
    await view.settle();
    await view.go('/?edit=b1');
    const options = [...view.input<HTMLSelectElement>('bank-question-topic').options];
    expect(options.find((o) => o.value === 't9')?.textContent?.trim()).toBe('Old units (archived)');
    expect(options.find((o) => o.value === 't9')?.disabled).toBe(true);
    view.input<HTMLFormElement>('bank-form').dispatchEvent(new Event('submit'));
    await view.settle();
    expect(view.fake.updateBankQuestion).not.toHaveBeenCalled();
    expect(view.root.textContent).toContain('This topic is archived. Choose an active topic.');
  });

  it.each([
    [
      new ApiError('full', 409, 'BANK_FULL'),
      'Your bank holds 2,000 questions, the most it can. Delete questions you no longer use first.',
    ],
    [
      new ApiError('bad topic', 422, 'INVALID_TOPIC'),
      'This topic can no longer be used. Choose an active topic.',
    ],
  ])('keeps the form and says why when the server refuses it (%s)', async (error, message) => {
    const view = render([], { addToBank: vi.fn(() => Promise.reject(error)) });
    await view.settle();
    await view.go('/?edit=new');
    view.type('bank-question-prompt', 'A third of 3?');
    view.type('bank-question-key', '1');
    view.type('bank-question-topic', 't1');
    view.input<HTMLFormElement>('bank-form').dispatchEvent(new Event('submit'));
    await view.settle();
    expect(view.page.state.error()).toBe(message);
    expect(view.page.view()).toBe('edit');
    expect(view.input<HTMLTextAreaElement>('bank-question-prompt').value).toBe('A third of 3?');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(true);
  });

  it('says a question deleted elsewhere is gone, keeping the changes', async () => {
    const view = render([question()], {
      updateBankQuestion: vi.fn(() => Promise.reject(new ApiError('not found', 404, 'NOT_FOUND'))),
    });
    await view.settle();
    await view.go('/?edit=b1');
    view.type('bank-question-prompt', 'Half of 2?');
    view.input<HTMLFormElement>('bank-form').dispatchEvent(new Event('submit'));
    await view.settle();
    expect(view.page.state.error()).toContain('This bank question no longer exists');
    expect(view.input<HTMLTextAreaElement>('bank-question-prompt').value).toBe('Half of 2?');
  });

  it('keeps an unsaved form when the teacher goes back to the list, and refuses to open another', async () => {
    const view = render([question(), question({ id: 'b2', prompt: 'Other' })]);
    await view.settle();
    await view.go('/?edit=b1');
    view.type('bank-question-prompt', 'Half of 3?');
    await view.go('/');
    expect(view.root.textContent).toContain('You have unsaved changes to “Half of 3?”.');
    view.input<HTMLButtonElement>('edit-b2').click();
    await view.settle();
    expect(view.page.state.error()).toBe('Save or discard the question you are editing first.');
    expect(view.page.editKey()).toBe('b1');
    expect(view.input<HTMLTextAreaElement>('bank-question-prompt').value).toBe('Half of 3?');
    [...view.root.querySelectorAll('#bank-form button')]
      .find((b) => b.textContent?.includes('Discard changes'))
      ?.dispatchEvent(new Event('click'));
    await view.settle();
    expect(view.page.view()).toBe('list');
    expect(TestBed.inject(UnsavedChanges).any()).toBe(false);
  });
});

describe('deleting a bank question', () => {
  it('confirms, says assessments keep their copies, deletes and re-reads the list', async () => {
    const view = render([question(), question({ id: 'b2', prompt: 'Other' })]);
    await view.settle();
    view.root.querySelector<HTMLButtonElement>('[aria-label="Delete Other"]')?.click();
    view.fixture.detectChanges();
    const dialog = view.root.querySelector('dialog');
    expect(dialog?.open).toBe(true);
    expect(dialog?.textContent).toContain('Delete this bank question?');
    expect(dialog?.textContent).toContain('Assessments that already use a copy of it keep theirs');
    view.held.items = [question()];
    [...(dialog?.querySelectorAll('button') ?? [])]
      .find((b) => b.textContent?.includes('Delete question'))
      ?.click();
    await vi.waitFor(() => expect(view.page.state.message()).toBe('Bank question deleted.'));
    await view.settle();
    expect(view.fake.removeBankQuestion).toHaveBeenCalledWith('b2');
    expect(dialog?.open).toBe(false);
    expect(view.rows().map((r) => r[0])).toEqual(['Half of 1?']);
  });

  it('says on the page when the list cannot be re-read after a delete', async () => {
    const view = render([question(), question({ id: 'b2', prompt: 'Other' })]);
    await view.settle();
    view.root.querySelector<HTMLButtonElement>('[aria-label="Delete Other"]')?.click();
    view.fixture.detectChanges();
    view.held.items = Promise.reject(new Error('Assessment is unavailable.'));
    view.held.items.catch(() => undefined);
    [...(view.root.querySelector('dialog')?.querySelectorAll('button') ?? [])]
      .find((b) => b.textContent?.includes('Delete question'))
      ?.click();
    await vi.waitFor(() => expect(view.page.state.error()).toContain('could not be refreshed'));
    await view.settle();
    expect(view.page.state.message()).toBe('Bank question deleted.');
    expect(view.rows().map((r) => r[0])).toEqual(['Half of 1?']);
  });
});
