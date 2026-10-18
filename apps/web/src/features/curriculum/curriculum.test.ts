// @vitest-environment jsdom
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { CurriculumApi } from '../../api/clients';
import type { Topic } from '../../api/models';
import { CurriculumNames } from '../../core/curriculum-names';
import { Session } from '../../core/session';
import { CurriculumComponent } from './curriculum';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const testing: Topic = { id: 't1', name: 'Testing' };
const unitTests: Topic = { id: 't2', name: 'Unit tests' };

function setup(archive = vi.fn(() => Promise.resolve())) {
  let topics = [testing, unitTests];
  const refresh = vi.fn(() => Promise.resolve(topics));
  const createTopic = vi.fn(async ({ name }: { name: string }) => {
    const topic = { id: 't3', name };
    topics = [...topics, topic];
    return topic;
  });
  archive.mockImplementation(async (id: string) => {
    topics = topics.filter((t) => t.id !== id);
  });
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: CurriculumApi, useValue: { archiveTopic: archive, createTopic } },
      { provide: CurriculumNames, useValue: { refresh } },
      { provide: Session, useValue: { isTeacher: signal(true) } },
    ],
  });
  TestBed.overrideComponent(CurriculumComponent, { set: { template: '', imports: [] } });
  const page = TestBed.createComponent(CurriculumComponent).componentInstance;
  page.load();
  return { page, archive, refresh, createTopic };
}

describe('curriculum topics', () => {
  it('adds a topic by name and lists it', async () => {
    const { page, createTopic } = setup();
    await vi.waitFor(() => expect(page.topics.value()).toHaveLength(2));
    page.form.setValue({ name: 'Mocks' });
    page.create();
    await vi.waitFor(() => expect(page.topics.value()).toHaveLength(3));
    expect(createTopic).toHaveBeenCalledWith({ name: 'Mocks' });
    expect(page.state.message()).toBe('Topic added.');
    expect(page.form.getRawValue()).toEqual({ name: '' });
  });

  it('archives a topic after it is confirmed and lists the rest', async () => {
    const { page, archive } = setup();
    await vi.waitFor(() => expect(page.topics.value()).toHaveLength(2));
    page.archive();
    expect(archive).not.toHaveBeenCalled();
    page.confirmArchive(unitTests);
    page.archive();
    await vi.waitFor(() => expect(page.topics.value()).toEqual([testing]));
    expect(archive).toHaveBeenCalledWith('t2');
    expect(page.state.message()).toBe('Unit tests archived.');
    expect(page.archiving()).toBeNull();
  });

  it('keeps the dialog open with the reason when archiving fails', async () => {
    const archive = vi.fn(() => Promise.resolve());
    const { page } = setup(archive);
    await vi.waitFor(() => expect(page.topics.value()).toHaveLength(2));
    archive.mockImplementation(() => Promise.reject(new Error('Curriculum is unavailable.')));
    page.confirmArchive(testing);
    page.archive();
    await vi.waitFor(() => expect(page.archiveState.error()).toBe('Curriculum is unavailable.'));
    expect(page.archiving()).toBe(testing);
    expect(page.topics.value()).toHaveLength(2);
  });

  it('shows on the page when the list cannot be refreshed after archiving', async () => {
    const { page, refresh } = setup();
    await vi.waitFor(() => expect(page.topics.value()).toHaveLength(2));
    refresh.mockRejectedValueOnce(new Error('Curriculum is unavailable.'));
    page.confirmArchive(unitTests);
    page.archive();
    await vi.waitFor(() => expect(page.state.error()).toBe('Curriculum is unavailable.'));
    expect(page.archiving()).toBeNull();
    expect(page.archiveState.error()).toBe('');
  });
});
