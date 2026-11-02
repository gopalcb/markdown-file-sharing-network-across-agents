import { Routes } from '@angular/router';
import { authGuard } from './guards/auth.guard';
export const routes: Routes = [
  ...['login', 'auth/callback', 'denied', 'signed-out'].map((path) => ({
    path: `work-organizer/${path}`,
    data: { title: 'Account', authMode: path === 'auth/callback' ? 'callback' : path },
    loadComponent: () => import('./components/auth/auth.component').then((m) => m.AuthComponent),
  })),
  {
    path: '',
    pathMatch: 'full',
    redirectTo: 'work-organizer/workspace/project-tracker',
  },
  {
    path: 'work-organizer',
    pathMatch: 'full',
    redirectTo: 'work-organizer/workspace/project-tracker',
  },
  {
    path: 'work-organizer/workspace/overview',
    pathMatch: 'full',
    redirectTo: 'work-organizer/workspace/project-tracker',
  },
  {
    path: 'work-organizer/workspace/project-tracker',
    data: { section: 'workspace', title: 'Project Tracker' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/project-tracker/project-tracker.component').then(
        (m) => m.ProjectTrackerComponent,
      ),
  },
  {
    path: 'work-organizer/workspace/project-tracker/new',
    data: { section: 'workspace', title: 'Create work item' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/work-item/work-item.component').then((m) => m.WorkItemComponent),
  },
  {
    path: 'work-organizer/workspace/project-tracker/:id',
    data: { section: 'workspace', title: 'Work item details' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/work-item/work-item.component').then((m) => m.WorkItemComponent),
  },
  {
    path: 'work-organizer/workspace/documents',
    data: { section: 'workspace', title: 'Documents' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/documents/documents.component').then((m) => m.DocumentsComponent),
  },
  {
    path: 'work-organizer/workspace/merger-agent',
    data: { section: 'workspace', title: 'Merger agent' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/merger-agent/merger-agent.component').then(
        (m) => m.MergerAgentComponent,
      ),
  },
  {
    path: 'work-organizer/workspace/messages',
    data: { section: 'workspace', title: 'Messages' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/messages/messages.component').then((m) => m.MessagesComponent),
  },
  ...['tasks', 'activity'].map((path) => ({
    path: `work-organizer/workspace/${path}`,
    redirectTo: 'work-organizer/workspace/project-tracker',
    pathMatch: 'full' as const,
  })),
  {
    path: 'work-organizer/analytics',
    pathMatch: 'full',
    redirectTo: 'work-organizer/analytics/gh-portfolio',
  },
  {
    path: 'work-organizer/analytics/gh-portfolio',
    data: { section: 'analytics', title: 'GH Portfolio' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/portfolio-analytics/portfolio-analytics.component').then(
        (m) => m.PortfolioAnalyticsComponent,
      ),
  },
  {
    path: 'work-organizer/design-system/components',
    data: { section: 'design-system', title: 'Cards & buttons' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/components/components.component').then((m) => m.ComponentsComponent),
  },
  {
    path: 'work-organizer/design-system/grid',
    data: { section: 'design-system', title: 'Responsive grid' },
    canActivate: [authGuard],
    loadComponent: () => import('./components/grid/grid.component').then((m) => m.GridComponent),
  },
  {
    path: 'work-organizer/design-system/forms',
    data: { section: 'design-system', title: 'Forms & inputs' },
    canActivate: [authGuard],
    loadComponent: () => import('./components/forms/forms.component').then((m) => m.FormsComponent),
  },
  {
    path: 'work-organizer/design-system/loading',
    data: { section: 'design-system', title: 'Loading states' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/loading/loading.component').then((m) => m.LoadingComponent),
  },
  {
    path: 'work-organizer/settings/preferences',
    data: { section: 'settings', title: 'Preferences' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/preferences/preferences.component').then((m) => m.PreferencesComponent),
  },
  {
    path: '**',
    data: { section: 'workspace', title: 'Page not found' },
    canActivate: [authGuard],
    loadComponent: () =>
      import('./components/not-found/not-found.component').then((m) => m.NotFoundComponent),
  },
];
