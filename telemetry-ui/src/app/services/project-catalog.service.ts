import { DestroyRef, Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { firstValueFrom, Subscription } from 'rxjs';
import { AuthService } from './auth.service';
import { ProjectTrackerStoreService } from './project-tracker-store.service';
import { Project, ProjectsService } from './projects.service';

/** Server catalog; legacy browser projects remain available for explicit import. */
@Injectable({ providedIn: 'root' })
export class ProjectCatalogService {
  private readonly auth = inject(AuthService);
  private readonly work = inject(ProjectTrackerStoreService);
  private readonly api = inject(ProjectsService);
  private request?: Subscription;
  private loadedKey: string | null = null;
  private readonly server = signal<Project[]>([]);
  private readonly legacy = signal<{ name: string; description: string }[]>([]);
  private readonly selection = signal('');
  readonly error = signal('');
  readonly loading = signal(false);
  readonly saving = signal(false);
  readonly cursor = signal<string | null>(null);
  readonly projects = computed(() => (this.loadedKey === this.key() ? this.server() : []));
  readonly legacyProjects = computed(() => {
    const known = new Set(this.projects().map((p) => p.name.toLocaleLowerCase()));
    const candidates = new Map(this.legacy().map((p) => [p.name, p]));

    for (const item of this.work.items()) {
      if (!candidates.has(item.project)) {
        candidates.set(item.project, { name: item.project, description: '' });
      }
    }

    return [...candidates.values()].filter((p) => !known.has(p.name.toLocaleLowerCase()));
  });
  readonly selected = computed(() =>
    this.projects().some((p) => p.name === this.selection())
      ? this.selection()
      : (this.projects()[0]?.name ?? ''),
  );
  constructor() {
    effect(() => {
      this.auth.identity();

      untracked(() => this.reload());
    });

    inject(DestroyRef).onDestroy(() => this.request?.unsubscribe());
  }
  private key(): string | null {
    const identity = this.auth.identity();

    return identity ? JSON.stringify([identity.issuer, identity.sub]) : null;
  }
  reload(): void {
    this.request?.unsubscribe();

    this.loadedKey = this.key();

    this.server.set([]);

    this.legacy.set([]);

    this.selection.set('');

    this.cursor.set(null);

    this.error.set('');

    if (!this.loadedKey) {
      this.loading.set(false);

      return;
    }

    try {
      const raw = localStorage.getItem('wo.projects.v1.' + this.loadedKey);

      if (raw && raw.length <= 300000) {
        const value = JSON.parse(raw);

        if (Array.isArray(value.projects) && value.projects.length <= 100) {
          this.legacy.set(
            value.projects.filter(
              (p: any) =>
                p &&
                typeof p.name === 'string' &&
                p.name.trim() &&
                p.name.length <= 80 &&
                typeof p.description === 'string' &&
                p.description.length <= 2000,
            ),
          );
        }
      }

      this.selection.set(localStorage.getItem('wo.project-selection.v1.' + this.loadedKey) ?? '');
    } catch {
      this.error.set(
        'Legacy browser projects could not be read. Their stored data has not been changed.',
      );
    }

    this.loadMore();
  }
  loadMore(): void {
    const key = this.key();

    if (!key) {
      return;
    }

    this.request?.unsubscribe();

    this.loading.set(true);
    const cursor = this.cursor();

    this.request = this.api.list(cursor).subscribe({
      next: (page) => {
        if (key !== this.key()) {
          return;
        }

        this.server.set([
          ...new Map(
            [...(cursor ? this.server() : []), ...page.projects].map((p) => [p.id, p]),
          ).values(),
        ]);

        this.cursor.set(page.cursor);

        this.loading.set(false);
      },
      error: () => {
        if (key !== this.key()) {
          return;
        }

        this.loading.set(false);

        this.error.set(
          'Projects could not be loaded from the server. Retry to refresh the dropdown.',
        );
      },
    });
  }
  async create(name: string, description: string): Promise<boolean> {
    name = name.trim();

    if (this.saving()) {
      return false;
    }

    if (!name || name.length > 80 || description.length > 2000) {
      this.error.set(
        'Enter a project name (1–80 characters) and description up to 2,000 characters.',
      );

      return false;
    }
    const key = this.key();

    if (!key || !this.auth.allowed()) {
      return false;
    }

    this.saving.set(true);

    this.error.set('');

    try {
      const result = await firstValueFrom(this.api.create(name, description));

      if (key !== this.key()) {
        return false;
      }

      this.server.update((projects) => [
        ...projects.filter((p) => p.id !== result.project.id),
        result.project,
      ]);

      this.select(result.project.name);

      return true;
    } catch (error: any) {
      if (key === this.key()) {
        this.error.set(
          error.status === 409
            ? 'A project with this name already exists. Refresh projects and select it.'
            : 'Project save was not confirmed. Refresh projects before retrying.',
        );
      }

      return false;
    } finally {
      this.saving.set(false);
    }
  }
  select(name: string): boolean {
    if (!this.projects().some((project) => project.name === name)) {
      return false;
    }

    this.selection.set(name);

    try {
      localStorage.setItem('wo.project-selection.v1.' + this.key(), name);
    } catch {
      /* Server project remains saved; selection is optional browser convenience. */
    }

    return true;
  }
}
