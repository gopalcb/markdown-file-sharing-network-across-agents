import {
  Injectable,
  InjectionToken,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from '@angular/core';
import { WorkItem, WorkItemDraft, WorkStatus } from '../interfaces/work-item.interface';
import { AuthService } from './auth.service';

export const WORK_STATUSES: { value: WorkStatus; label: string }[] = [
  { value: 'pending', label: 'Pending' },
  { value: 'in-progress', label: 'In Progress' },
  { value: 'done', label: 'Done' },
];
/** Replace this adapter when a server work-item API is available. Never stores credentials. */
export const PROJECT_TRACKER_STORAGE = new InjectionToken<() => Storage>(
  'Project Tracker storage',
  {
    providedIn: 'root',
    factory: () => () => localStorage,
  },
);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export function validDraft(value: unknown): value is WorkItemDraft {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const item = value as WorkItemDraft;

  return (
    typeof item.title === 'string' &&
    item.title.trim().length > 0 &&
    item.title.length <= 120 &&
    typeof item.project === 'string' &&
    item.project.trim().length > 0 &&
    item.project.length <= 80 &&
    typeof item.description === 'string' &&
    item.description.length <= 20000 &&
    (item.priority === undefined || ['High', 'Medium', 'Low'].includes(item.priority)) &&
    (item.due === undefined || item.due === '' || /^\d{4}-\d{2}-\d{2}$/.test(item.due)) &&
    (item.assigned_to === undefined ||
      (typeof item.assigned_to === 'string' && item.assigned_to.length <= 120)) &&
    (item.created_at === undefined ||
      (typeof item.created_at === 'string' && !Number.isNaN(Date.parse(item.created_at)))) &&
    WORK_STATUSES.some((status) => status.value === item.status)
  );
}

@Injectable({ providedIn: 'root' })
export class ProjectTrackerStoreService {
  private readonly auth = inject(AuthService);
  private readonly storage = inject(PROJECT_TRACKER_STORAGE);
  private readonly savedItems = signal<WorkItem[]>([]);
  private readonly loadedKey = signal<string | null>(null);
  private snapshot: string | null = null;
  private readable = false;
  readonly error = signal('');
  readonly items = computed(() => (this.key() === this.loadedKey() ? this.savedItems() : []));

  constructor() {
    effect(() => {
      this.auth.identity();

      untracked(() => this.synchronize());
    });
  }
  private key(): string | null {
    const identity = this.auth.identity();

    return identity
      ? 'wo.project-tracker.v1.' + JSON.stringify([identity.issuer, identity.sub])
      : null;
  }
  private synchronize(): void {
    if (this.key() !== this.loadedKey()) {
      this.reload();
    }
  }
  reload(): void {
    const key = this.key();

    this.loadedKey.set(key);

    this.savedItems.set([]);

    this.error.set('');

    this.readable = false;

    this.snapshot = null;

    if (!key) {
      return;
    }

    try {
      const raw = this.storage().getItem(key);

      if (raw && raw.length > 5000000) {
        throw new Error('Oversized data');
      }
      const data: unknown = raw ? JSON.parse(raw) : [];

      if (
        !Array.isArray(data) ||
        data.length > 200 ||
        data.some(
          (item) =>
            !validDraft(item) ||
            !('id' in item) ||
            typeof item.id !== 'string' ||
            !UUID.test(item.id),
        ) ||
        new Set(data.map((item) => item.id)).size !== data.length
      ) {
        throw new Error('Invalid data');
      }

      // Project only known fields from persisted data.
      this.savedItems.set(
        data.map(
          ({
            id,
            title,
            project,
            description,
            status,
            priority,
            due,
            assigned_to,
            created_at,
          }) => ({
            id,
            title,
            project,
            description,
            status,
            ...(priority !== undefined ? { priority } : {}),
            ...(due !== undefined ? { due } : {}),
            ...(assigned_to !== undefined ? { assigned_to } : {}),
            ...(created_at !== undefined ? { created_at } : {}),
          }),
        ),
      );

      this.snapshot = raw;

      this.readable = true;
    } catch {
      this.error.set(
        'Saved work could not be read. Check browser storage and retry; existing data has not been replaced.',
      );
    }
  }
  save(draft: WorkItemDraft, id?: string): WorkItem | null {
    this.synchronize();
    const key = this.loadedKey();

    if (!key || !this.auth.allowed()) {
      this.error.set('Sign in to save work.');

      return null;
    }

    if (!this.readable) {
      return null;
    }

    if (!validDraft(draft)) {
      this.error.set(
        'Enter a title (1–120 characters), project (1–80), valid status and description up to 20,000 characters.',
      );

      return null;
    }

    if (id !== undefined && (!UUID.test(id) || !this.savedItems().some((item) => item.id === id))) {
      this.error.set('This work item is no longer available.');

      return null;
    }

    if (!id && this.savedItems().length >= 200) {
      this.error.set('This browser has reached the limit of 200 work items.');

      return null;
    }

    try {
      const item: WorkItem = {
        id: id ?? crypto.randomUUID(),
        title: draft.title.trim(),
        project: draft.project.trim(),
        description: draft.description,
        status: draft.status,
        priority:
          draft.priority ??
          this.savedItems().find((entry) => entry.id === id)?.priority ??
          'Medium',
        assigned_to:
          draft.assigned_to ??
          this.savedItems().find((entry) => entry.id === id)?.assigned_to ??
          this.auth.identity()?.displayName ??
          '',
        created_at:
          this.savedItems().find((entry) => entry.id === id)?.created_at ??
          new Date().toISOString(),
        due: draft.due ?? this.savedItems().find((entry) => entry.id === id)?.due ?? '',
      };
      const next = id
        ? this.savedItems().map((entry) => (entry.id === id ? item : entry))
        : [...this.savedItems(), item];
      const storage = this.storage();

      if (storage.getItem(key) !== this.snapshot) {
        this.error.set('Work changed in another tab. Reload saved work before saving again.');

        return null;
      }
      const raw = JSON.stringify(next);

      if (raw.length > 5000000) {
        this.error.set(
          'Saved work exceeds the browser storage limit. Shorten the description before saving.',
        );

        return null;
      }

      storage.setItem(key, raw);

      this.snapshot = raw;

      this.savedItems.set(next);

      this.error.set('');

      return item;
    } catch {
      this.error.set(
        'Work could not be saved. Browser storage may be blocked or full. Your changes are still in the form.',
      );

      return null;
    }
  }
  remove(id: string): boolean {
    this.synchronize();
    const key = this.loadedKey();

    if (
      !key ||
      !this.auth.allowed() ||
      !this.readable ||
      !this.items().some((item) => item.id === id)
    ) {
      return false;
    }

    try {
      const storage = this.storage();

      if (storage.getItem(key) !== this.snapshot) {
        this.error.set('Work changed in another tab. Reload saved work before deleting.');

        return false;
      }
      const next = this.items().filter((item) => item.id !== id);
      const raw = JSON.stringify(next);

      storage.setItem(key, raw);

      this.snapshot = raw;

      this.savedItems.set(next);

      this.error.set('');

      return true;
    } catch {
      this.error.set('Work could not be deleted. Browser storage may be blocked.');

      return false;
    }
  }
  transition(id: string, status: WorkStatus): boolean {
    this.synchronize();
    const item = this.items().find((entry) => entry.id === id);

    return item ? this.save({ ...item, status }, id) !== null : false;
  }
}
