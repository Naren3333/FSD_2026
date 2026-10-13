import { Component, HostListener, OnInit, computed, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';
import { SECTIONS } from './app.routes';
import { AiUsage } from './core/ai-usage';
import { ClassroomContext } from './core/classroom-context';
import { Session } from './core/session';
import { UnsavedChanges } from './core/unsaved-changes';
import { UsageComponent } from './features/usage/usage';
import { ActionState } from './shared/action-state';
import { StatusComponent } from './shared/status';

/** The workspace shell: navigation, the signed-in user, the current classroom and the open section. */
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
  readonly state = new ActionState();
  readonly sections = computed(() =>
    SECTIONS.filter((section) => !section.teacherOnly || this.session.isTeacher()),
  );

  constructor() {
    // A section may have spent AI allowance (indexing, chat) since the meter last loaded.
    inject(Router)
      .events.pipe(
        filter((event) => event instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe(() => {
        if (this.session.profile()) this.usage.refresh();
      });
  }

  ngOnInit(): void {
    void this.state.run(async () => {
      await this.session.ready();
      if (this.session.profile()) await this.context.refresh();
    });
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
