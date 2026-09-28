import { bootstrapApplication } from '@angular/platform-browser';
import { AppComponent } from './app';
bootstrapApplication(AppComponent).catch(() => { document.body.textContent = 'The workspace could not start. Refresh to retry.'; });

