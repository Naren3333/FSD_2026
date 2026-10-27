// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import type { Assessment, DocumentRecord, Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { ActionState } from '../../shared/action-state';
import { Section } from '../../shared/section';
import { MAX_QUESTIONS, assessmentForm } from './editor';
import { AssessmentEditorComponent } from './editor-view';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));
afterEach(() => vi.useRealTimers());

/* the editor's logic (no template) over a new assessment's form. */
function setup(archived: Topic[] = []) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      {
        provide: CurriculumNames,
        useValue: {
          topics: signal([]),
          archived: signal(archived),
          topicName: (id: string) => (id === 's1' ? 'Fractions' : ''),
        },
      },
    ],
  });
  TestBed.overrideComponent(AssessmentEditorComponent, { set: { template: '', imports: [] } });
  const editor = TestBed.createComponent(AssessmentEditorComponent).componentInstance;
  const form = assessmentForm();
  const state = new ActionState();
  editor.form = form;
  editor.state = state;
  editor.classroomId = 'c1';
  editor.documents = new Section<DocumentRecord[]>();
  editor.topicsState = new Section<Topic[]>();
  return { editor, form, state };
}

describe('the assessment editor', () => {
  it('adds questions with the last one’s topic, up to the limit, and keeps at least one', () => {
    vi.useFakeTimers();
    const { editor, form } = setup();
    editor.questions.at(0).controls.topic_id.setValue('s1');
    editor.addQuestion();
    expect(editor.questions.length).toBe(2);
    expect(editor.questions.at(1).controls.topic_id.value).toBe('s1');
    expect(form.dirty).toBe(true);
    // the new question is open and the first, untouched and invalid, stays open too.
    expect(editor.isCollapsed(editor.questions.at(1))).toBe(false);
    while (editor.questions.length < MAX_QUESTIONS) editor.addQuestion();
    editor.addQuestion();
    expect(editor.questions.length).toBe(MAX_QUESTIONS);
    while (editor.questions.length > 1) editor.removeQuestion(0);
    editor.removeQuestion(0);
    expect(editor.questions.length).toBe(1);
  });

  it('leaves out an archived topic for a new question', () => {
    vi.useFakeTimers();
    const { editor } = setup([{ id: 'old', name: 'Old fractions' }]);
    editor.questions.at(0).controls.topic_id.setValue('old');
    expect(editor.archivedTopic('old')).toBe('Old fractions');
    editor.addQuestion();
    expect(editor.questions.at(1).controls.topic_id.value).toBe('');
  });

  it('collapses and expands a question, and opens any with an error for validation', () => {
    const { editor } = setup();
    const question = editor.questions.at(0);
    editor.toggleCollapsed(question);
    expect(editor.isCollapsed(question)).toBe(true);
    editor.toggleCollapsed(question);
    expect(editor.isCollapsed(question)).toBe(false);
    editor.toggleCollapsed(question);
    // a blank prompt is an error.
    editor.expandInvalid();
    expect(editor.isCollapsed(question)).toBe(false);
  });

  it('adds and removes rubric criteria, keeping at least one', () => {
    vi.useFakeTimers();
    const { editor } = setup();
    const question = editor.questions.at(0);
    question.controls.type.setValue('free_response');
    const start = question.controls.criteria.length;
    editor.addCriterion(question, 0);
    expect(question.controls.criteria.length).toBe(start + 1);
    while (question.controls.criteria.length > 1) editor.removeCriterion(question, 0);
    editor.removeCriterion(question, 0);
    expect(question.controls.criteria.length).toBe(1);
  });

  it('describes the assessment for its heading, outline and save bar', () => {
    const { editor, form } = setup();
    expect(editor.editorTitle()).toBe('Create an assessment');
    editor.editing = { id: 'a1', title: 'Fractions' } as Assessment;
    expect(editor.editorTitle()).toBe('Edit draft: Fractions');
    editor.editingPublished = true;
    expect(editor.editorTitle()).toBe('Edit assessment: Fractions');

    const question = editor.questions.at(0);
    expect(editor.promptLine(question)).toBe('Untitled question');
    question.controls.prompt.setValue('x'.repeat(80) + '\nsecond line');
    expect(editor.promptLine(question)).toBe('x'.repeat(57) + '…');
    question.controls.points.setValue(3);
    expect(editor.size()).toBe('1 question · 3 marks');
    expect(editor.typeName('free_response')).toBe('Written response');
    expect(editor.topicName('s1')).toBe('Fractions');
    expect(editor.dueLine()).toBe('No due date');
    form.controls.due_at.setValue('2026-11-03T17:00');
    expect(editor.dueLine()).toMatch(/^Due 3 Nov 2026/);
  });

  it('opens and closes the ai drafting panel, moving focus with it', () => {
    vi.useFakeTimers();
    const { editor } = setup();
    document.body.innerHTML =
      '<button id="generator-toggle"></button><h3 id="generator-heading" tabindex="-1"></h3>';
    editor.toggleGenerator();
    vi.runAllTimers();
    expect(editor.generatorOpen()).toBe(true);
    expect(document.activeElement?.id).toBe('generator-heading');
    editor.toggleGenerator();
    vi.runAllTimers();
    expect(editor.generatorOpen()).toBe(false);
    expect(document.activeElement?.id).toBe('generator-toggle');
    document.body.innerHTML = '';
  });
});
