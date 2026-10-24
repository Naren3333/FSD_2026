// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it } from 'vitest';
import { ICONS, IconComponent } from './icon';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

function render(name: string, size?: number) {
  TestBed.resetTestingModule();
  const fixture = TestBed.createComponent(IconComponent);
  fixture.componentRef.setInput('name', name);
  if (size !== undefined) fixture.componentRef.setInput('size', size);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

describe('icon', () => {
  it.each(Object.keys(ICONS))('renders %s as a hidden, unfocusable svg', (name) => {
    const svg = render(name).querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg?.getAttribute('aria-hidden')).toBe('true');
    expect(svg?.getAttribute('focusable')).toBe('false');
    expect(svg?.getAttribute('viewBox')).toBe('0 0 24 24');
    expect(svg?.getAttribute('stroke')).toBe('currentColor');
    expect(svg?.querySelectorAll('path').length).toBeGreaterThan(0);
  });

  it('is 18px unless sized', () => {
    expect(render('check').querySelector('svg')?.getAttribute('width')).toBe('18');
    expect(render('check', 14).querySelector('svg')?.getAttribute('height')).toBe('14');
  });

  it('renders nothing for an unknown name', () => {
    expect(render('nope').querySelector('svg')).toBeNull();
    expect(render('toString').querySelector('svg')).toBeNull();
  });
});
