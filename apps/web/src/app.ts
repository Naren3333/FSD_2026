import {
  Component,
  DestroyRef,
  ElementRef,
  HostListener,
  OnInit,
  computed,
  effect,
  inject,
  signal,
  untracked,
  ViewChild,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';
import { GradingApi } from './api/clients';
import { navigation } from './app.routes';
import { AiUsage } from './core/ai-usage';
import { ClassroomContext } from './core/classroom-context';
import { sectionOf } from './core/section-shown';
import { Session } from './core/session';
import { UnsavedChanges } from './core/unsaved-changes';
import { UsageComponent } from './features/usage/usage';
import { ActionState } from './shared/action-state';
import { IconComponent } from './shared/icon';
import { RefreshButtonComponent } from './shared/refresh-button';
import { Section } from './shared/section';
import { StatusComponent } from './shared/status';

/* the workspace shell: navigation, the signed-in user, the current classroom and the open section. */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    RouterOutlet,
    RouterLink,
    RouterLinkActive,
    StatusComponent,
    UsageComponent,
    IconComponent,
    RefreshButtonComponent,
  ],
  templateUrl: './app.html',
})
export class AppComponent implements OnInit {
  readonly session = inject(Session);
  readonly context = inject(ClassroomContext);
  private readonly unsaved = inject(UnsavedChanges);
  private readonly usage = inject(AiUsage);
  private readonly router = inject(Router);
  private readonly grading = inject(GradingApi);
  readonly state = new ActionState();
  /* the last load failed, so the route guards may have refused the address. */
  private failed = false;
  readonly sections = computed(() => {
    const role = this.session.profile()?.role;
    return role === 'teacher' || role === 'student' ? navigation(role) : [];
  });
  /* the list loaded and is empty, for either role. */
  readonly noClassrooms = computed(() => this.context.known() && this.context.classrooms().length === 0);
  /* draft grades waiting in the current classroom, shown beside grading review. */
  private readonly drafts = new Section<number>();
  readonly draftCount = computed(() => this.drafts.value() ?? 0);
  /* the phone drawer holding the sidebar. */
  readonly menuOpen = signal(false);
  @ViewChild('brand') private brand?: ElementRef<HTMLElement>;
  @ViewChild('menuButton') private menuButton?: ElementRef<HTMLElement>;

  constructor() {
    let section: string | null = null;
    this.router.events
      .pipe(
        filter((event): event is NavigationEnd => event instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe((event) => {
        if (!this.session.profile()) return;
        if (this.menuOpen()) this.closeMenu();
        // a page changing only its own address (opening a grade) has not left the section.
        const now = sectionOf(event.urlAfterRedirects);
        if (now === section) return;
        section = now;
        // a section may have spent ai allowance (indexing, chat) since the meter last loaded.
        this.usage.refresh();
        // grading review may have changed the drafts.
        this.loadDrafts();
      });
    effect(() => {
      this.context.classroomId();
      this.session.isTeacher();
      untracked(() => {
        this.drafts.clear();
        this.loadDrafts();
      });
    });
    // a drawer left open while the window widens would keep the page inert.
    if (typeof matchMedia === 'function') {
      const wide = matchMedia('(min-width: 900px)');
      const close = () => wide.matches && this.menuOpen.set(false);
      wide.addEventListener('change', close);
      inject(DestroyRef).onDestroy(() => wide.removeEventListener('change', close));
    }
  }

  private loadDrafts(): void {
    const classroomId = this.context.classroomId();
    if (!this.session.isTeacher() || !classroomId) return;
    void this.drafts.load(async () => {
      const { drafts } = await this.grading.counts(classroomId);
      return typeof drafts === 'number' ? drafts : 0;
    });
  }

  openMenu(): void {
    this.menuOpen.set(true);
    // focus moves once the drawer is visible.
    setTimeout(() => this.brand?.nativeElement.focus());
  }

  closeMenu(): void {
    this.menuOpen.set(false);
    setTimeout(() => this.menuButton?.nativeElement.focus());
  }

  @HostListener('document:keydown.escape')
  escape(): void {
    if (this.menuOpen()) this.closeMenu();
  }

  ngOnInit(): void {
    this.load();
  }

  /* loads the signed-in user's profile and classrooms; also the retry when that fails. */
  load(): void {
    const retry = this.failed;
    void this.state
      .run(async () => {
        await this.session.ready();
        const profile = this.session.profile();
        if (!profile) return;
        this.context.setUser(profile.id);
        // each section that needs a classroom shows this failure in place, with try again.
        await this.context.refresh().catch(() => undefined);
        // the route guards refused the address while the profile was missing; open it now that the role is
        // known.
        if (retry) await this.router.navigateByUrl(location.pathname + location.search);
      })
      .then(() => (this.failed = !!this.state.error()));
  }

  login(): void {
    void this.state.run(() => this.session.login());
  }

  logout(): void {
    if (this.hasUnsaved()) {
      this.state.error.set('Finish or clear your unsaved forms before signing out.');
      return;
    }
    void this.state.run(() => this.session.logout());
  }

  /* students have no classrooms page, so they reload the picker here after a teacher enrolls them. */
  refreshClassrooms(): void {
    void this.state.run(() => this.context.refresh());
  }

  selectClassroom(event: Event): void {
    this.context.select((event.target as HTMLSelectElement).value);
  }

  hasUnsaved(): boolean {
    return this.unsaved.any();
  }

  /* whether unsaved work would be lost by changing classrooms. */
  classroomLocked(): boolean {
    return this.unsaved.locksClassroom();
  }

  @HostListener('window:beforeunload', ['$event'])
  beforeUnload(event: BeforeUnloadEvent): void {
    if (this.hasUnsaved()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
}
