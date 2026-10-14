import { Type, inject } from '@angular/core';
import { CanActivateFn, CanMatchFn, ResolveFn, Router, Routes } from '@angular/router';
import { Session } from './core/session';

/** A workspace section: its route, its navigation label, and the component loaded the first time it opens. */
export interface Section {
  path: string;
  label: string;
  /** The label students see, when it differs. */
  studentLabel?: string;
  teacherOnly?: boolean;
  studentOnly?: boolean;
  load: () => Promise<Type<unknown>>;
}

export const SECTIONS: Section[] = [
  {
    path: 'classrooms',
    label: 'Classrooms',
    teacherOnly: true,
    load: () => import('./features/classrooms/classrooms').then((m) => m.ClassroomsComponent),
  },
  {
    path: 'curriculum',
    label: 'Curriculum',
    teacherOnly: true,
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
    path: 'results',
    label: 'My results',
    studentOnly: true,
    load: () => import('./features/results/results').then((m) => m.ResultsComponent),
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
    studentLabel: 'My progress',
    load: () => import('./features/performance/performance').then((m) => m.PerformanceComponent),
  },
];

/** The section label for the signed-in user's role. */
export function sectionLabel(section: Section, session: Session): string {
  return session.isStudent() && section.studentLabel ? section.studentLabel : section.label;
}

/** Where each role starts: teachers on their classrooms, students on their assessments. */
function landing(session: Session): string {
  return session.isStudent() ? '/assessments' : '/classrooms';
}

/** Sections for one role send the other role to its own landing page. Signed out, the shell shows sign-in. */
function onlyFor(role: 'teacher' | 'student'): CanMatchFn {
  return async () => {
    const session = inject(Session);
    await session.ready().catch(() => undefined);
    const profile = session.profile();
    return !profile || profile.role === role || inject(Router).parseUrl(landing(session));
  };
}

const toLanding: CanActivateFn = async () => {
  const session = inject(Session);
  await session.ready().catch(() => undefined);
  return inject(Router).parseUrl(landing(session));
};

function title(section: Section): ResolveFn<string> {
  return () => `${sectionLabel(section, inject(Session))} · Learning Ledger`;
}

export const routes: Routes = [
  { path: '', pathMatch: 'full', canActivate: [toLanding], children: [] },
  ...SECTIONS.map((section) => ({
    path: section.path,
    title: title(section),
    loadComponent: section.load,
    canMatch: section.teacherOnly ? [onlyFor('teacher')] : section.studentOnly ? [onlyFor('student')] : [],
    data: { keepAlive: true },
  })),
  { path: '**', canActivate: [toLanding], children: [] },
];
