import { Component, ElementRef, HostListener, ViewChild, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs';
import { MONITORING_PATH } from '../../app.routes';
import { Session } from '../../core/session';
import { ActionState } from '../../shared/action-state';
import { Drawer } from '../../shared/drawer';
import { IconComponent } from '../../shared/icon';
import { StatusComponent } from '../../shared/status';

/* the platform operator's shell: monitoring only, with no classroom, profile or ai allowance. */
@Component({
  selector: 'app-operator-shell',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive, StatusComponent, IconComponent],
  templateUrl: './operator-shell.html',
})
export class OperatorShellComponent {
  readonly session = inject(Session);
  readonly state = new ActionState();
  readonly monitoringPath = MONITORING_PATH;
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
