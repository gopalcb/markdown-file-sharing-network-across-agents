import { Injectable, computed, signal } from '@angular/core';
import { WorkItem } from '../interfaces/demo-work-item.interface';

/** Tab-local demo state; navigation preserves it, reload clears it. */
@Injectable({ providedIn: 'root' })
export class WorkStoreService {
  readonly items = signal<WorkItem[]>([]);
  readonly activity = signal<string[]>([]);
  readonly completed = computed(() => this.items().filter((item) => item.done).length);
  private sequence = 0;
  reset(): void {
    this.items.set([]);

    this.activity.set([]);

    this.sequence = 0;
  }
  add(name: string, email: string): void {
    this.items.update((items) => [...items, { id: ++this.sequence, name, email, done: false }]);

    this.activity.update((events) => [`Added “${name}”`, ...events]);
  }
  toggle(id: number): void {
    const item = this.items().find((item) => item.id === id);

    if (!item) {
      return;
    }

    this.items.update((items) =>
      items.map((entry) => (entry.id === id ? { ...entry, done: !entry.done } : entry)),
    );

    this.activity.update((events) => [
      `${item.done ? 'Reopened' : 'Completed'} “${item.name}”`,
      ...events,
    ]);
  }
}
