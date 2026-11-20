import { Component, DestroyRef, ElementRef, ViewChild, computed, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { AdminApi } from '../../api/clients';
import type { Account, AccountPage, CreatedAccount, PersonRole } from '../../api/models';
import { UnsavedChanges } from '../../core/unsaved-changes';
import { ActionState } from '../../shared/action-state';
import { ConfirmDialogComponent } from '../../shared/confirm-dialog';
import { FieldComponent, FieldControlDirective } from '../../shared/field';
import { PageHeaderComponent } from '../../shared/page-header';
import { studentName } from '../../shared/names';
import { readPage } from '../../shared/paged-section';
import { PagerComponent } from '../../shared/pager';
import { RefreshButtonComponent } from '../../shared/refresh-button';
import { Section } from '../../shared/section';
import { SectionStateComponent } from '../../shared/section-state';
import { StatusComponent } from '../../shared/status';
import { requiredText } from '../../shared/validators';

/* a new account's username, as identity accepts it: 3–64 letters, digits, dots, hyphens or underscores, starting with a letter or digit. */
export const USERNAME = /^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/;

const ROLES: { id: PersonRole; label: string; plural: string }[] = [
  { id: 'teacher', label: 'Teacher', plural: 'Teachers' },
  { id: 'student', label: 'Student', plural: 'Students' },
];

/* the school admin's accounts (adr 0019): create a teacher's or student's sign-in account with a temporary password, and list the school's accounts. */
@Component({
  selector: 'app-admin-accounts',
  standalone: true,
  imports: [
    PageHeaderComponent,
    RefreshButtonComponent,
    ReactiveFormsModule,
    FieldComponent,
    FieldControlDirective,
    StatusComponent,
    SectionStateComponent,
    PagerComponent,
    ConfirmDialogComponent,
  ],
  templateUrl: './accounts.html',
})
export class AdminAccountsComponent {
  private readonly admin = inject(AdminApi);
  private readonly unsaved = inject(UnsavedChanges);
  readonly roles = ROLES;
  readonly studentName = studentName;
  readonly state = new ActionState();
  readonly form = new FormGroup({
    role: new FormControl<PersonRole>('student', { nonNullable: true, validators: Validators.required }),
    first: new FormControl('', { nonNullable: true, validators: requiredText(60) }),
    last: new FormControl('', { nonNullable: true, validators: requiredText(60) }),
    username: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(USERNAME)],
    }),
  });
  /* the account just created, or whose password was just reset, with its temporary password, shown until the admin is done with it. */
  readonly created = signal<CreatedAccount | null>(null);
  /* whether that password replaced an existing account's, rather than came with a new account. */
  readonly passwordReset = signal(false);
  readonly copied = signal('');
  @ViewChild('createdHeading') private createdHeading?: ElementRef<HTMLElement>;

  /* the account the reset dialog asks about. */
  readonly resetting = signal<Account | null>(null);
  readonly resetState = new ActionState();
  @ViewChild('resetDialog') resetDialog?: ConfirmDialogComponent;

  /* the accounts list: which role, the search, and the page on show. */
  readonly role = signal<PersonRole>('teacher');
  readonly search = signal('');
  readonly page = signal(0);
  readonly pageSize = 20;
  readonly accounts = new Section<AccountPage>();
  private searchTimer?: ReturnType<typeof setTimeout>;
  readonly roleLabel = computed(() => ROLES.find((r) => r.id === this.role())!.plural);

  constructor() {
    // a shown temporary password counts as unsaved, so leaving the page warns before it is lost.
    this.unsaved.track(() => this.form.dirty || this.created() !== null);
    // a search typed just before leaving is not sent after.
    inject(DestroyRef).onDestroy(() => clearTimeout(this.searchTimer));
    this.load();
  }

  create(): void {
    if (!this.state.validate(this.form, 'account-form')) return;
    const { role, first, last, username } = this.form.getRawValue();
    void this.state.run(async () => {
      const account = await this.admin.createAccount({
        role,
        username: username.trim(),
        first_name: first.trim(),
        last_name: last.trim(),
      });
      this.created.set(account);
      this.passwordReset.set(false);
      this.copied.set('');
      this.form.reset({ role, first: '', last: '', username: '' });
      // the list reports its own failure: the account exists either way, and its password is on show.
      if (this.role() === role) this.load();
      setTimeout(() => this.createdHeading?.nativeElement.focus());
    }, 'Account created.');
  }

  /* copies the temporary password, saying whether it worked: a browser may refuse the clipboard. */
  async copy(password: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(password);
      this.copied.set('Temporary password copied.');
    } catch {
      this.copied.set('Copying was refused by the browser. Select the password and copy it instead.');
    }
  }

  /* forgets the temporary password; it cannot be shown again. */
  done(): void {
    this.created.set(null);
    this.passwordReset.set(false);
    this.copied.set('');
  }

  /* the account's name, or its username before it has one. */
  accountName(account: Account): string {
    return studentName(account) || 'this account';
  }

  confirmReset(account: Account): void {
    this.resetting.set(account);
    this.resetDialog?.show();
  }

  closeReset(): void {
    this.resetting.set(null);
    this.resetDialog?.close();
  }

  /* gives the account a new temporary password and shows it once, as for a new account: how an admin recovers an account whose password was lost. */
  resetPassword(): void {
    const account = this.resetting();
    if (!account) return;
    void this.resetState.run(async () => {
      const result = await this.admin.resetPassword(account.id);
      this.closeReset();
      this.created.set(result);
      this.passwordReset.set(true);
      this.copied.set('');
      setTimeout(() => this.createdHeading?.nativeElement.focus());
    });
  }

  showRole(role: PersonRole): void {
    if (this.role() === role) return;
    this.role.set(role);
    this.page.set(0);
    this.load();
  }

  searchFor(words: string): void {
    this.search.set(words);
    clearTimeout(this.searchTimer);
    this.searchTimer = setTimeout(() => {
      this.page.set(0);
      this.load();
    }, 250);
  }

  load(): void {
    void this.accounts.load((current) => this.read(current));
  }

  private async read(current: () => boolean): Promise<AccountPage> {
    const role = this.role();
    const search = this.search().trim();
    const { result, page } = await readPage(
      this.page(),
      this.pageSize,
      (offset, limit) => this.admin.accounts(role, search, offset, limit),
      (r) => !r.accounts.length,
    );
    if (current()) this.page.set(page);
    return result;
  }
}
