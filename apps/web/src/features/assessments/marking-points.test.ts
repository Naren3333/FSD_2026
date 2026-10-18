// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { FormControl, FormGroup } from '@angular/forms';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it } from 'vitest';
import type { DocumentRecord, Excerpt } from '../../api/models';
import { MAX_EXCERPT_TEXT, markWrittenBy } from './editor';
import { MarkingPointsComponent } from './marking-points';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

const scheme: DocumentRecord = {
  id: 'd1',
  classroom_id: 'c1',
  title: 'Mark scheme',
  mime_type: 'application/pdf',
  status: 'indexed',
  revision: 1,
  doc_type: 'marking_scheme',
};

function setup() {
  TestBed.resetTestingModule();
  TestBed.overrideComponent(MarkingPointsComponent, { set: { template: '', imports: [] } });
  const points = TestBed.createComponent(MarkingPointsComponent).componentInstance;
  points.control = new FormControl<Excerpt[]>([], { nonNullable: true });
  points.documents = [scheme];
  points.ngOnInit();
  return points;
}

describe('a question’s marking points', () => {
  it('adds written points up to the limit, and removes them', () => {
    const points = setup();
    points.written.setValue('  Accept O2.  ');
    points.add();
    expect(points.control.value).toEqual([{ text: 'Accept O2.' }]);
    expect(points.control.dirty).toBe(true);
    expect(points.written.value).toBe('');
    expect(points.source(points.control.value[0])).toBe('Written by you');
    points.written.setValue('x'.repeat(MAX_EXCERPT_TEXT + 1));
    points.add();
    expect(points.control.value).toHaveLength(1);
    points.written.setValue('   ');
    points.add();
    expect(points.control.value).toHaveLength(1);
    for (let i = 0; i < 6; i++) {
      points.written.setValue(`Point ${i}`);
      points.add();
    }
    expect(points.control.value).toHaveLength(5);
    expect(points.full()).toBe(true);
    points.remove(0);
    expect(points.control.value.map((e) => e.text)).toEqual(['Point 0', 'Point 1', 'Point 2', 'Point 3']);
    expect(points.full()).toBe(false);
  });

  it('counts a point written but not added as unsaved work in the editor', () => {
    const points = setup();
    const editor = new FormGroup({ marking_scheme: points.control });
    points.written.setValue('   ');
    expect(editor.dirty).toBe(false);
    points.written.setValue('Accept O2.');
    expect(editor.dirty).toBe(true);
    expect(points.control.value).toEqual([]);
  });

  it('shows points added from outside, and who wrote them', () => {
    const points = setup();
    const drafted = { text: '1 mark: names a unit test.' };
    markWrittenBy(points.control, drafted, 'ai');
    points.control.setValue([drafted, { text: 'Saved earlier.' }]);
    expect(points.excerpts()).toHaveLength(2);
    expect(points.source(drafted)).toBe('Written by AI');
    expect(points.source({ text: 'Saved earlier.' })).toBe('Written for this question');
  });

  it('still names the document a point was copied from before schemes were retired', () => {
    const points = setup();
    expect(points.source({ document_id: 'd1', chunk_id: 'k1', page: 3, text: 'Award the mark.' })).toBe(
      'Mark scheme, page 3',
    );
  });
});
