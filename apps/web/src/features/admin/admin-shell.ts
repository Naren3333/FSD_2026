import { Component, ElementRef, HostListener, ViewChild, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';
import { ADMIN_ACCOUNTS_PATH, ADMIN_CLASSROOMS_PATH, MONITORING_PATH } from '../../app.routes';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { Drawer } from '../../shared/drawer';
import { IconComponent } from '../../shared/icon';
import { StatusComponent } from '../../shared/status';

/* the school admin's shell (adr 0019): the school's classrooms, its accounts and monitoring, with no current classroom, profile or ai allowance. */
@Component({
  selector: 'app-admin-shell',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive, StatusComponent, IconComponent],
  templateUrl: './admin-shell.html',
})
export class AdminShellComponent {
  readonly session = inject(Session);
  readonly state = new ActionState();
  readonly links = [
    { path: ADMIN_CLASSROOMS_PATH, label: 'Classrooms', icon: 'classrooms' },
    { path: ADMIN_ACCOUNTS_PATH, label: 'Accounts', icon: 'students' },
    { path: MONITORING_PATH, label: 'Monitoring', icon: 'dashboard' },
  ] as const;
  @ViewChild('brand') private brand?: ElementRef<HTMLElement>;
  @ViewChild('menuButton') private menuButton?: ElementRef<HTMLElement>;
  readonly drawer = new Drawer(
    () => this.brand?.nativeElement,
    () => this.menuButton?.nativeElement,
  );

  constructor() {
    inject(Router)
      .events.pipe(
        filter((event) => event instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe(() => {
        if (this.drawer.open()) this.drawer.hide();
      });
  }

  @HostListener('document:keydown.escape')
  escape(): void {
    if (this.drawer.open()) this.drawer.hide();
  }

  login(): void {
    void this.state.run(() => this.session.login());
  }

  logout(): void {
    void this.state.run(() => this.session.logout());
  }
}
