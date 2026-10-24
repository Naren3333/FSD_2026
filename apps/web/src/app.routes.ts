import { Type, inject } from '@angular/core';
import { CanActivateFn, CanMatchFn, ResolveFn, Router, Routes } from '@angular/router';
import { Session } from './core/session';
import type { IconName } from './shared/icon';

/* a workspace section: its route, its navigation label, and the component loaded the first time it opens. */
export interface WorkspaceSection {
  path: string;
  label: string;
  /* the label students see, when it differs. */
  studentLabel?: string;
  icon: IconName;
  /* the navigation group it sits in, '' for the first, unlabelled one. */
  group: string;
  /* the group students see it in, when it differs. */
  studentGroup?: string;
  /* false keeps it out of the grouped navigation (profile sits in the account block). */
  nav?: false;
  teacherOnly?: boolean;
  studentOnly?: boolean;
  load: () => Promise<Type<unknown>>;
}

export const SECTIONS: WorkspaceSection[] = [
  {
    path: 'home',
    label: 'Home',
    studentOnly: true,
    icon: 'home',
    group: '',
    load: () => import('./features/home/home').then((m) => m.HomeComponent),
  },
  {
    path: 'dashboard',
    label: 'Class dashboard',
    teacherOnly: true,
    icon: 'dashboard',
    group: '',
    load: () => import('./features/dashboard/dashboard').then((m) => m.DashboardComponent),
  },
  {
    path: 'assessments',
    label: 'Assessments',
    icon: 'assessments',
    group: 'Teach',
    studentGroup: 'My work',
    load: () => import('./features/assessments/assessments').then((m) => m.AssessmentsComponent),
  },
  {
    path: 'materials',
    label: 'Teaching materials',
    icon: 'materials',
    group: 'Teach',
    studentGroup: 'Learn',
    load: () => import('./features/materials/materials').then((m) => m.MaterialsComponent),
  },
  {
    path: 'curriculum',
    label: 'Curriculum',
    teacherOnly: true,
    icon: 'curriculum',
    group: 'Teach',
    load: () => import('./features/curriculum/curriculum').then((m) => m.CurriculumComponent),
  },
  {
    path: 'results',
    label: 'My results',
    studentOnly: true,
    icon: 'results',
    group: 'My work',
    load: () => import('./features/results/results').then((m) => m.ResultsComponent),
  },
  {
    path: 'review',
    label: 'Grading review',
    teacherOnly: true,
    icon: 'grading',
    group: 'Review',
    load: () => import('./features/review/review').then((m) => m.ReviewComponent),
  },
  {
    path: 'performance',
    label: 'Learning performance',
    studentLabel: 'My progress',
    icon: 'performance',
    group: 'Review',
    studentGroup: 'My work',
    load: () => import('./features/performance/performance').then((m) => m.PerformanceComponent),
  },
  {
    path: 'chat',
    label: 'Ask the materials',
    icon: 'chat',
    group: 'Assistant',
    studentGroup: 'Learn',
    load: () => import('./features/chat/chat').then((m) => m.ChatComponent),
  },
  {
    path: 'classrooms',
    label: 'Classrooms',
    teacherOnly: true,
    icon: 'classrooms',
    group: 'Setup',
    load: () => import('./features/classrooms/classrooms').then((m) => m.ClassroomsComponent),
  },
  {
    path: 'profile',
    label: 'Profile',
    icon: 'profile',
    group: '',
    nav: false,
    load: () => import('./features/profile/profile').then((m) => m.ProfileComponent),
  },
];

/* the section label for the signed-in user's role. */
export function sectionLabel(section: WorkspaceSection, session: Session): string {
  return session.isStudent() && section.studentLabel ? section.studentLabel : section.label;
}

/* the order of each role's navigation groups. */
const GROUPS = { teacher: ['', 'Teach', 'Review', 'Assistant', 'Setup'], student: ['', 'My work', 'Learn'] };

export interface NavigationGroup {
  label: string;
  sections: { path: string; label: string; icon: IconName }[];
}

/* the sections a role sees in its navigation, grouped and in order. */
export function navigation(role: 'teacher' | 'student'): NavigationGroup[] {
  const student = role === 'student';
  const shown = SECTIONS.filter(
    (section) => section.nav !== false && !(student ? section.teacherOnly : section.studentOnly),
  );
  return GROUPS[role]
    .map((label) => ({
      label,
      sections: shown
        .filter((section) => ((student && section.studentGroup) || section.group) === label)
        .map((section) => ({
          path: section.path,
          label: (student && section.studentLabel) || section.label,
          icon: section.icon,
        })),
    }))
    .filter((group) => group.sections.length > 0);
}

/* where each role starts: teachers on their class dashboard, students on their home. */
function landing(session: Session): string {
  return session.isStudent() ? '/home' : '/dashboard';
}

/* signed in, but the profile (and so the role) did not load: nothing opens until the shell's try again works. */
function profileMissing(session: Session): boolean {
  return session.signedIn && !session.profile();
}

/* sections for one role send the other role to its own landing page. */
function onlyFor(role: 'teacher' | 'student'): CanMatchFn {
  return async () => {
    const session = inject(Session);
    const router = inject(Router);
    await session.ready().catch(() => undefined);
    if (profileMissing(session)) return false;
    const profile = session.profile();
    return !profile || profile.role === role || router.parseUrl(landing(session));
  };
}

const toLanding: CanActivateFn = async () => {
  // inject() only works before the first await.
  const session = inject(Session);
  const router = inject(Router);
  await session.ready().catch(() => undefined);
  if (profileMissing(session)) return false;
  return router.parseUrl(landing(session));
};

function title(section: WorkspaceSection): ResolveFn<string> {
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
