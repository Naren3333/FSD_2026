// @vitest-environment jsdom
import { TestBed } from '@angular/core/testing';
import { BrowserTestingModule, platformBrowserTesting } from '@angular/platform-browser/testing';
import { readFileSync } from 'node:fs';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { ActionState } from './action-state';
import { ConfirmDialogComponent } from './confirm-dialog';
import { StatusComponent } from './status';

const template = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8');

beforeAll(() => TestBed.initTestEnvironment(BrowserTestingModule, platformBrowserTesting()));

// jsdom has no modal dialogs: these do what a browser's do for the parts the component relies on.
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function (this: HTMLDialogElement) {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function (this: HTMLDialogElement) {
    if (!this.open) return;
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
});

function render(tone?: 'default' | 'danger') {
  TestBed.resetTestingModule();
  TestBed.overrideComponent(ConfirmDialogComponent, { set: { template: template('./confirm-dialog.html') } });
  TestBed.overrideComponent(StatusComponent, { set: { template: template('./status.html') } });
  const fixture = TestBed.createComponent(ConfirmDialogComponent);
  const state = new ActionState();
  fixture.componentRef.setInput('dialogId', 'remove');
  fixture.componentRef.setInput('heading', 'Remove this student?');
  fixture.componentRef.setInput('confirmLabel', 'Remove student');
  fixture.componentRef.setInput('state', state);
  if (tone) fixture.componentRef.setInput('tone', tone);
  fixture.detectChanges();
  const root = fixture.nativeElement as HTMLElement;
  const dialog = root.querySelector('dialog') as HTMLDialogElement;
  const buttons = [...dialog.querySelectorAll('button')];
  const closed = vi.fn();
  const confirmed = vi.fn();
  fixture.componentInstance.closed.subscribe(closed);
  fixture.componentInstance.confirm.subscribe(confirmed);
  /* presses escape as a browser does: a cancel event, then closing unless it was prevented. */
  const escape = () => {
    const cancel = new Event('cancel', { cancelable: true });
    dialog.dispatchEvent(cancel);
    if (!cancel.defaultPrevented) dialog.close();
  };
  return { fixture, state, dialog, buttons, closed, confirmed, escape };
}

describe('confirmation dialog', () => {
  it('is labelled by its heading and puts Cancel first, as the initially focused action', () => {
    const { dialog, buttons } = render();
    expect(dialog.getAttribute('aria-labelledby')).toBe('remove-title');
    expect(dialog.querySelector('#remove-title')?.textContent).toBe('Remove this student?');
    expect(buttons.map((b) => b.textContent?.trim())).toEqual(['Cancel', 'Remove student']);
    expect(buttons[0].hasAttribute('autofocus')).toBe(true);
  });

  it('opens modal without the action’s earlier error or message', () => {
    const { fixture, state, dialog } = render();
    state.error.set('Identity is unavailable.');
    state.message.set('Student removed.');
    fixture.componentInstance.show();
    fixture.detectChanges();
    expect(dialog.open).toBe(true);
    expect(state.error()).toBe('');
    expect(dialog.textContent).not.toContain('Identity is unavailable.');
  });

  it('closes on Escape and reports it, without confirming', () => {
    const { fixture, dialog, closed, confirmed, escape } = render();
    fixture.componentInstance.show();
    escape();
    expect(dialog.open).toBe(false);
    expect(closed).toHaveBeenCalledTimes(1);
    expect(confirmed).not.toHaveBeenCalled();
  });

  it('stays open on Escape while its action runs, with both buttons disabled', () => {
    const { fixture, state, dialog, buttons, closed, escape } = render();
    fixture.componentInstance.show();
    state.busy.set(true);
    fixture.detectChanges();
    escape();
    expect(dialog.open).toBe(true);
    expect(closed).not.toHaveBeenCalled();
    expect(buttons.every((b) => b.disabled)).toBe(true);
  });

  it('closes on Cancel, and confirms only on the action', () => {
    const { fixture, dialog, buttons, closed, confirmed } = render();
    fixture.componentInstance.show();
    buttons[1].click();
    expect(confirmed).toHaveBeenCalledTimes(1);
    expect(dialog.open).toBe(true);
    buttons[0].click();
    expect(dialog.open).toBe(false);
    expect(closed).toHaveBeenCalledTimes(1);
  });

  it('styles the action as danger only for a danger tone, keeping Cancel secondary', () => {
    expect(render().buttons[1].classList).not.toContain('danger');
    const { buttons } = render('danger');
    expect(buttons[1].classList).toContain('danger');
    expect(buttons[0].classList).toContain('secondary');
    expect(buttons[0].hasAttribute('autofocus')).toBe(true);
  });

  it('keeps its live region empty and inactive while there is nothing to announce', () => {
    const { fixture, state, dialog } = render();
    fixture.componentInstance.show();
    fixture.detectChanges();
    // mounted before anything is announced, so screen readers hear the first change; it takes no space.
    expect(dialog.querySelector('[role="status"]')?.textContent?.trim()).toBe('');
    expect(dialog.querySelector('.feedback')?.classList).not.toContain('active');
    state.busy.set(true);
    fixture.detectChanges();
    expect(dialog.querySelector('[role="status"]')?.textContent?.trim()).toBe('Working…');
  });

  it('shows the action’s failure inside the dialog', () => {
    const { fixture, state, dialog } = render();
    fixture.componentInstance.show();
    state.error.set('Identity is unavailable.');
    fixture.detectChanges();
    expect(dialog.querySelector('[role="alert"]')?.textContent).toBe('Identity is unavailable.');
  });
});
