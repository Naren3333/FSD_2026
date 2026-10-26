// @vitest-environment jsdom
import { Component, inject } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it } from 'vitest';
import { UnsavedChanges } from './unsaved-changes';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

let dirty = false;
@Component({ selector: 'app-form-section', standalone: true, template: '' })
class FormSection {
  constructor() {
    inject(UnsavedChanges).track(() => dirty);
  }
}

describe('UnsavedChanges', () => {
  it('reports a section’s unsaved work until that section is destroyed', () => {
    TestBed.resetTestingModule();
    const unsaved = TestBed.inject(UnsavedChanges);
    expect(unsaved.any()).toBe(false);
    const section = TestBed.createComponent(FormSection);
    dirty = false;
    expect(unsaved.any()).toBe(false);
    dirty = true;
    expect(unsaved.any()).toBe(true);
    section.destroy();
    expect(unsaved.any()).toBe(false);
  });

  it('keeps the classroom locked only for work a classroom change would lose', () => {
    TestBed.resetTestingModule();
    const unsaved = TestBed.inject(UnsavedChanges);
    let draft = true;
    TestBed.runInInjectionContext(() => unsaved.track(() => draft, { locksClassroom: false }));
    // a kept-per-classroom draft still warns on unload and sign-out, but leaves the classroom free.
    expect(unsaved.any()).toBe(true);
    expect(unsaved.locksClassroom()).toBe(false);
    dirty = true;
    TestBed.createComponent(FormSection);
    expect(unsaved.locksClassroom()).toBe(true);
    dirty = false;
    draft = false;
    expect(unsaved.any()).toBe(false);
  });
});
