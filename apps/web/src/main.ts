import { inject, provideAppInitializer } from '@angular/core';
import { bootstrapApplication } from '@angular/platform-browser';
import { RouteReuseStrategy, provideRouter } from '@angular/router';
import { AppComponent } from './app';
import { routes } from './app.routes';
import { Auth } from './core/auth';
import { APP_CONFIG, loadConfig } from './core/config';
import { KeepAliveStrategy } from './core/keep-alive';

// The stack's own gateway and Keycloak come first: Auth and the Gateway read them as they are created.
loadConfig()
  .then((config) =>
    bootstrapApplication(AppComponent, {
      providers: [
        { provide: APP_CONFIG, useValue: config },
        // Keycloak reads and clears the sign-in redirect's parameters before the router sees the URL.
        // If Keycloak is unreachable the app still starts, signed out; signing in then reports the error.
        provideAppInitializer(() =>
          inject(Auth)
            .initialize()
            .catch(() => undefined),
        ),
        provideRouter(routes),
        { provide: RouteReuseStrategy, useClass: KeepAliveStrategy },
      ],
    }),
  )
  .catch(() => {
    document.body.textContent = 'The workspace could not start. Refresh to retry.';
  });
