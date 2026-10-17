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
});
