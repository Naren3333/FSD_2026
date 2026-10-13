import { Type, inject } from '@angular/core';
import { CanMatchFn, Router, Routes } from '@angular/router';
import { Session } from './core/session';

/** A workspace section: its route, its navigation label, and the component loaded the first time it opens. */
export interface Section {
  path: string;
  label: string;
  teacherOnly?: boolean;
  load: () => Promise<Type<unknown>>;
}

export const SECTIONS: Section[] = [
  {
    path: 'classrooms',
    label: 'Classrooms',
    load: () => import('./features/classrooms/classrooms').then((m) => m.ClassroomsComponent),
  },
  {
    path: 'curriculum',
    label: 'Curriculum',
    load: () => import('./features/curriculum/curriculum').then((m) => m.CurriculumComponent),
  },
  {
    path: 'materials',
    label: 'Teaching materials',
    load: () => import('./features/materials/materials').then((m) => m.MaterialsComponent),
  },
  {
    path: 'chat',
    label: 'Ask the materials',
    load: () => import('./features/chat/chat').then((m) => m.ChatComponent),
  },
  {
    path: 'assessments',
    label: 'Assessments',
    load: () => import('./features/assessments/assessments').then((m) => m.AssessmentsComponent),
  },
  {
    path: 'review',
    label: 'Grading review',
    teacherOnly: true,
    load: () => import('./features/review/review').then((m) => m.ReviewComponent),
  },
  {
    path: 'performance',
    label: 'Learning performance',
    load: () => import('./features/performance/performance').then((m) => m.PerformanceComponent),
  },
];

/** Teacher-only sections send everyone else to Classrooms. */
const teacherOnly: CanMatchFn = async () => {
  const session = inject(Session);
  await session.ready().catch(() => undefined);
  return session.isTeacher() || inject(Router).parseUrl('/classrooms');
};

export const routes: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'classrooms' },
  ...SECTIONS.map((section) => ({
    path: section.path,
    title: `${section.label} · Learning Ledger`,
    loadComponent: section.load,
    canMatch: section.teacherOnly ? [teacherOnly] : [],
    data: { keepAlive: true },
  })),
  { path: '**', redirectTo: 'classrooms' },
];
