import {
  Component,
  ElementRef,
  HostListener,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Title } from '@angular/platform-browser';
import {
  NavigationCancel,
  NavigationEnd,
  NavigationError,
  NavigationStart,
  Router,
  RouterLink,
  RouterLinkActive,
  RouterOutlet,
} from '@angular/router';
import { WoSpinnerComponent } from '../../../theme/components';

import { AuthService } from '../../services/auth.service';

export const navigation = [
  {
    id: 'workspace',
    label: 'Workspace',
    items: [
      { slug: 'project-tracker', label: 'Project Tracker' },
      { slug: 'documents', label: 'Documents' },
      { slug: 'merger-agent', label: 'Merger agent' },
    ],
  },
  {
    id: 'analytics',
    label: 'Analytics',
    items: [{ slug: 'gh-portfolio', label: 'GH Portfolio' }],
  },
  {
    id: 'design-system',
    label: 'Design system',
    items: [
      { slug: 'components', label: 'Cards & buttons' },
      { slug: 'grid', label: 'Responsive grid' },
      { slug: 'forms', label: 'Forms & inputs' },
      { slug: 'loading', label: 'Loading states' },
    ],
  },
  {
    id: 'settings',
    label: 'Settings',
    items: [{ slug: 'preferences', label: 'Preferences' }],
  },
];
@Component({
  selector: 'app-root',
  imports: [RouterLink, RouterLinkActive, RouterOutlet, WoSpinnerComponent],
  templateUrl: './app.component.html',
})
export class AppComponent {
  readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly title = inject(Title);
  @ViewChild('pageTitle') pageTitle?: ElementRef<HTMLHeadingElement>;
  readonly headerNavigation = [
    { label: 'Projects', path: '/work-organizer/workspace/project-tracker' },
    { label: 'Documents', path: '/work-organizer/workspace/documents' },
    { label: 'Merger agent', path: '/work-organizer/workspace/merger-agent' },
    { label: 'Messages', path: '/work-organizer/workspace/messages' },
    { label: 'Analytics', path: '/work-organizer/analytics/gh-portfolio' },
  ];
  readonly accountOpen = signal(false);
  readonly authPage = signal(false);
  readonly showShell = computed(() => this.auth.allowed() && !this.authPage());
  @ViewChild('accountButton') accountButton?: ElementRef<HTMLButtonElement>;
  @ViewChild('accountRegion') accountRegion?: ElementRef<HTMLElement>;

  @HostListener('document:click', ['$event'])
  dismissOutside(event: MouseEvent): void {
    if (this.accountOpen() && !this.accountRegion?.nativeElement.contains(event.target as Node)) {
      this.accountOpen.set(false);
    }
  }

  @HostListener('document:keydown.escape')
  dismissAccount(): void {
    if (!this.accountOpen()) {
      return;
    }

    this.accountOpen.set(false);

    this.accountButton?.nativeElement.focus();
  }

  logout(): void {
    this.accountOpen.set(false);

    this.auth.logout();
  }
  readonly section = signal('workspace');
  readonly heading = signal('Overview');
  readonly busy = signal(false);
  readonly current = computed(
    () => navigation.find((group) => group.id === this.section()) ?? navigation[0],
  );
  skipToContent(event: Event): void {
    event.preventDefault();

    this.pageTitle?.nativeElement.focus();
  }
  constructor() {
    this.router.events.pipe(takeUntilDestroyed()).subscribe((event) => {
      if (event instanceof NavigationStart) {
        this.busy.set(true);
      }

      if (event instanceof NavigationEnd) {
        const data = this.router.routerState.snapshot.root.firstChild?.data ?? {};

        this.authPage.set(Boolean(data['authMode']));

        this.accountOpen.set(false);

        this.section.set(data['section'] ?? 'workspace');

        this.heading.set(data['title'] ?? 'Overview');

        this.title.setTitle(`${this.heading()} · Telemetry`);

        this.busy.set(false);

        requestAnimationFrame(() => this.pageTitle?.nativeElement.focus({ preventScroll: true }));
      }

      if (event instanceof NavigationError || event instanceof NavigationCancel) {
        this.busy.set(false);
      }
    });
  }
}
