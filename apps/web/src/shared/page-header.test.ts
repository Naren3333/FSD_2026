// @vitest-environment jsdom
import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it } from 'vitest';
import { PageHeaderComponent } from './page-header';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

@Component({
  standalone: true,
  imports: [PageHeaderComponent],
  template: `<app-page-header title="Assessments" [subtitle]="subtitle"
    ><button>Act</button></app-page-header
  >`,
})
class HostComponent {
  subtitle = 'One short line.';
}

function render(subtitle: string) {
  TestBed.resetTestingModule();
  TestBed.overrideComponent(PageHeaderComponent, { set: { template: template('./page-header.html') } });
  const fixture = TestBed.createComponent(HostComponent);
  fixture.componentInstance.subtitle = subtitle;
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('page header', () => {
  it('renders the title as the page heading, the subtitle and the projected actions', () => {
    const root = render('One short line.');
    expect(root.querySelector('h2')?.textContent).toBe('Assessments');
    expect(root.querySelector('.page-header-subtitle')?.textContent).toBe('One short line.');
    expect(root.querySelector('.page-header-actions button')?.textContent).toBe('Act');
  });

  it('leaves out an empty subtitle', () => {
    expect(render('').querySelector('.page-header-subtitle')).toBeNull();
  });
});
