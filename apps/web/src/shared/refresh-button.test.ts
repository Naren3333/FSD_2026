// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { RefreshButtonComponent } from './refresh-button';

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

function render(inputs: { disabled?: boolean; busy?: boolean } = {}) {
  TestBed.resetTestingModule();
  const fixture = TestBed.createComponent(RefreshButtonComponent);
  fixture.componentRef.setInput('label', 'Refresh assessments');
  for (const [name, value] of Object.entries(inputs)) fixture.componentRef.setInput(name, value);
  fixture.detectChanges();
  const refreshed = vi.fn();
  fixture.componentInstance.refresh.subscribe(refreshed);
  const button = (fixture.nativeElement as HTMLElement).querySelector('button') as HTMLButtonElement;
  return { button, refreshed };
}

describe('refresh button', () => {
  it('is an icon button named by its label', () => {
    const { button } = render();
    expect(button.getAttribute('aria-label')).toBe('Refresh assessments');
    expect(button.title).toBe('Refresh assessments');
    expect(button.classList).toContain('icon-button');
    expect(button.querySelector('svg')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('emits refresh when pressed', () => {
    const { button, refreshed } = render();
    button.click();
    expect(refreshed).toHaveBeenCalledTimes(1);
  });

  it('can be disabled, and spins while busy', () => {
    const { button, refreshed } = render({ disabled: true, busy: true });
    expect(button.disabled).toBe(true);
    expect(button.classList).toContain('busy');
    button.click();
    expect(refreshed).not.toHaveBeenCalled();
  });
});
