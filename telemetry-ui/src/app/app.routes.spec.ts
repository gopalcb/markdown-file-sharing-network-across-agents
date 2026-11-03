import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { of } from 'rxjs';
import { ProjectsService } from './services/projects.service';
import { TasksService } from './services/tasks.service';
import { routes } from './app.routes';
import { PortfolioAnalyticsService } from './services/portfolio-analytics.service';
import { AppComponent } from './components/app/app.component';
import { AuthService } from './services/auth.service';
import { DocumentsService } from './services/documents.service';

describe('Work Organizer navigation', () => {
  beforeEach(() =>
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes),
        provideHttpClient(),
        { provide: ProjectsService, useValue: { list: () => of({ projects: [], cursor: null }) } },
        { provide: TasksService, useValue: { list: () => of({ tasks: [], cursor: null }) } },
        {
          provide: DocumentsService,
          useValue: {
            list: () => of({ documents: [], cursor: null }),
            read: () => of({ document: null }),
          },
        },
        {
          provide: PortfolioAnalyticsService,
          useValue: {
            page: () => of({ events: [], next_cursor: null, as_of: '2026-10-29T12:00:00Z' }),
          },
        },
        {
          provide: AuthService,
          useValue: {
            allowed: signal(true),
            identity: signal({
              issuer: 'https://example.com/pool',
              sub: 'alice',
              displayName: 'Alice',
            }),
            displayName: signal('Alice'),
            hasSession: () => true,
            logout: () => {},
          },
        },
      ],
    }),
  );

  it('opens a deep link and changes contextual navigation with the header section', async () => {
    const fixture = TestBed.createComponent(AppComponent);

    fixture.detectChanges();
    const router = TestBed.inject(Router);

    await router.navigateByUrl('/work-organizer/design-system/grid');

    fixture.detectChanges();

    await fixture.whenStable();

    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('h1').textContent.trim()).toBe('Responsive grid');

    expect(fixture.nativeElement.querySelector('.wo-footer').textContent).toContain(
      'Design system',
    );

    expect(fixture.nativeElement.querySelector('.wo-sidebar')).toBeNull();
    const skipEvent = new MouseEvent('click', { cancelable: true });

    fixture.nativeElement.querySelector('.skip-link').dispatchEvent(skipEvent);

    expect(skipEvent.defaultPrevented).toBe(true);

    expect(router.url).toBe('/work-organizer/design-system/grid');

    await router.navigateByUrl('/work-organizer/workspace/tasks');

    fixture.detectChanges();

    await fixture.whenStable();

    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('h1').textContent.trim()).toBe('Project Tracker');

    expect(fixture.nativeElement.querySelector('.wo-header-nav').textContent).toContain(
      'Documents',
    );

    expect(fixture.nativeElement.querySelector('.wo-header-nav').textContent).not.toContain(
      'Loading states',
    );
  });

  it('opens GH Portfolio by default within Analytics and selects its contextual menu', async () => {
    const fixture = TestBed.createComponent(AppComponent);
    const router = TestBed.inject(Router);

    fixture.detectChanges();

    await router.navigateByUrl('/work-organizer/analytics');

    fixture.detectChanges();

    await fixture.whenStable();

    fixture.detectChanges();

    expect(router.url).toBe('/work-organizer/analytics/gh-portfolio');

    expect(fixture.nativeElement.querySelector('.wo-header-nav a.active').textContent).toContain(
      'Analytics',
    );

    expect(fixture.nativeElement.querySelector('.wo-sidebar')).toBeNull();

    expect(fixture.nativeElement.querySelector('h1').textContent.trim()).toBe('GH Portfolio');

    expect(fixture.nativeElement.textContent).toContain(
      'No portfolio activity has been recorded yet.',
    );
  });

  it('opens Merger agent after Documents with source and merged-document tabs', async () => {
    const fixture = TestBed.createComponent(AppComponent);

    fixture.detectChanges();

    await TestBed.inject(Router).navigateByUrl('/work-organizer/workspace/merger-agent');

    await fixture.whenStable();

    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;
    const headerLabels = Array.from(root.querySelectorAll('.wo-header-nav a')).map((link) =>
      link.textContent?.trim(),
    );

    expect(headerLabels.indexOf('Merger agent')).toBe(headerLabels.indexOf('Documents') + 1);

    expect(root.querySelector('.wo-header-nav a.active')?.textContent).toContain('Merger agent');

    expect(
      Array.from(root.querySelectorAll('[role="tab"]')).map((tab) => tab.textContent?.trim()),
    ).toEqual(['Documents', 'Merged documents']);

    expect(root.textContent).toContain('Select documents to begin');
  });

  it('opens Messages as an inert preview with no sending or persistence', async () => {
    const fixture = TestBed.createComponent(AppComponent);

    fixture.detectChanges();

    await TestBed.inject(Router).navigateByUrl('/work-organizer/workspace/messages');

    await fixture.whenStable();

    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;

    expect(root.querySelector('.wo-header-nav a.active')?.textContent).toContain('Messages');

    expect(root.querySelector('form')).toBeNull();

    expect(
      Array.from(root.querySelectorAll<HTMLButtonElement>('.wo-main button')).every(
        (button) => button.disabled,
      ),
    ).toBe(true);

    expect(root.querySelector<HTMLTextAreaElement>('.wo-main textarea')?.disabled).toBe(true);

    expect(root.textContent).toContain('Live delivery is not connected.');
  });

  it('shows a useful not-found page for an unknown path', async () => {
    const fixture = TestBed.createComponent(AppComponent);

    fixture.detectChanges();

    await TestBed.inject(Router).navigateByUrl('/work-organizer/not-here');

    fixture.detectChanges();

    await fixture.whenStable();

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('This page isn’t here');

    expect(fixture.componentInstance.busy()).toBe(false);
  });
});
