// @vitest-environment jsdom
import { Component, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import { FieldComponent, FieldControlDirective } from './field';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

@Component({
  standalone: true,
  imports: [FieldComponent, FieldControlDirective],
  template: `<app-field controlId="name" label="Name" [hint]="hint()" [invalid]="invalid()">
    <input appFieldControl id="name" />
  </app-field>`,
})
class HostComponent {
  readonly hint = signal('');
  readonly invalid = signal(false);
}

function render() {
  TestBed.resetTestingModule();
  TestBed.overrideComponent(FieldComponent, { set: { template: template('./field.html') } });
  const fixture = TestBed.createComponent(HostComponent);
  fixture.detectChanges();
  const input = (fixture.nativeElement as HTMLElement).querySelector('input') as HTMLInputElement;
  return { fixture, input };
}

describe('field control', () => {
  it('is described by the hint only when there is one', () => {
    const { fixture, input } = render();
    expect(input.getAttribute('aria-describedby')).toBeNull();
    fixture.componentInstance.hint.set('At most 120 characters.');
    fixture.detectChanges();
    expect(input.getAttribute('aria-describedby')).toBe('name-help');
  });

  it('is marked invalid and described by the error while the field shows it', () => {
    const { fixture, input } = render();
    expect(input.getAttribute('aria-invalid')).toBeNull();
    fixture.componentInstance.invalid.set(true);
    fixture.detectChanges();
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('name-help');
    fixture.componentInstance.invalid.set(false);
    fixture.detectChanges();
    expect(input.getAttribute('aria-invalid')).toBeNull();
  });
});
