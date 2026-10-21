import { Component, HostListener, OnInit, computed, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';
import { SECTIONS, sectionLabel } from './app.routes';
import { AiUsage } from './core/ai-usage';
import { ClassroomContext } from './core/classroom-context';
import { Session } from './core/session';
import { UnsavedChanges } from './core/unsaved-changes';
import { UsageComponent } from './features/usage/usage';
import { ActionState } from './shared/action-state';
import { StatusComponent } from './shared/status';

/* the workspace shell: navigation, the signed-in user, the current classroom and the open section. */
@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive, StatusComponent, UsageComponent],
  templateUrl: './app.html',
})
export class AppComponent implements OnInit {
  readonly session = inject(Session);
  readonly context = inject(ClassroomContext);
  private readonly unsaved = inject(UnsavedChanges);
  private readonly usage = inject(AiUsage);
  private readonly router = inject(Router);
  readonly state = new ActionState();
  /* the last load failed, so the route guards may have refused the address. */
  private failed = false;
  readonly sections = computed(() =>
    SECTIONS.filter(
      (section) =>
        (!section.teacherOnly || this.session.isTeacher()) &&
        (!section.studentOnly || this.session.isStudent()),
    ).map((section) => ({ path: section.path, label: sectionLabel(section, this.session) })),
  );

  constructor() {
    // a section may have spent ai allowance (indexing, chat) since the meter last loaded.
    this.router.events
      .pipe(
        filter((event) => event instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe(() => {
        if (this.session.profile()) this.usage.refresh();
      });
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
        if (!this.session.profile()) return;
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

  @HostListener('window:beforeunload', ['$event'])
  beforeUnload(event: BeforeUnloadEvent): void {
    if (this.hasUnsaved()) {
      event.preventDefault();
      event.returnValue = '';
    }
  }
}
