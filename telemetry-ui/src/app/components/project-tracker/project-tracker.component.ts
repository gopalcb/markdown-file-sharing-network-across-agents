import {
  Component,
  ElementRef,
  ViewChild,
  ViewChildren,
  QueryList,
  computed,
  inject,
  signal,
  DestroyRef,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { AuthService } from '../../services/auth.service';
import { TasksService, ServerTask } from '../../services/tasks.service';
import { MarkdownEditorComponent } from '../markdown-editor/markdown-editor.component';
import { SelectComponent } from '../select/select.component';
import { FormsModule } from '@angular/forms';
import { WorkItem, WorkItemDraft, WorkStatus } from '../../interfaces/work-item.interface';
import { ProjectCatalogService } from '../../services/project-catalog.service';
import {
  ProjectTrackerStoreService,
  WORK_STATUSES,
} from '../../services/project-tracker-store.service';

@Component({
  imports: [FormsModule, MarkdownEditorComponent, SelectComponent],
  templateUrl: './project-tracker.component.html',
})
export class ProjectTrackerComponent {
  readonly store = inject(ProjectTrackerStoreService);
  readonly catalog = inject(ProjectCatalogService);
  readonly columns = WORK_STATUSES;
  readonly auth = inject(AuthService);
  private readonly tasksApi = inject(TasksService);
  private readonly destroy = inject(DestroyRef);
  readonly remote = signal<ServerTask[]>([]);
  readonly remoteError = signal('');
  readonly busy = signal(false);
  readonly taskCursor = signal<string | null>(null);
  readonly remoteProject = signal('');
  readonly allProjects = computed(() => [
    ...new Set([
      ...this.catalog.projects().map((p) => p.name),
      ...this.remote().map((t) => t.project),
      ...this.store.items().map((item) => item.project),
    ]),
  ]);
  readonly selectedProject = computed(
    () => this.remoteProject() || this.catalog.selected() || this.allProjects()[0] || '',
  );
  readonly projectOptions = computed(() =>
    this.allProjects().map((value) => ({ value, label: value })),
  );
  readonly itemProjectOptions = computed(() =>
    this.catalog.projects().map((project) => ({ value: project.name, label: project.name })),
  );
  readonly closing = signal(false);
  private closeTimer?: ReturnType<typeof setTimeout>;
  @ViewChildren(SelectComponent) selects!: QueryList<SelectComponent>;
  closeItem(): void {
    if (this.closing()) {
      return;
    }

    this.selects?.forEach((select) => select.close());

    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
      this.finishClose();

      return;
    }

    this.closing.set(true);

    this.closeTimer = setTimeout(() => this.finishClose(), 240);
  }
  finishClose(): void {
    clearTimeout(this.closeTimer);

    this.itemDialog.nativeElement.close();

    this.closing.set(false);

    this.activeTask = null;
  }
  readonly statusOptions = WORK_STATUSES;
  readonly priorityOptions = ['Medium', 'High', 'Low'].map((value) => ({ value, label: value }));
  activeTask: ServerTask | null = null;
  constructor() {
    this.loadTasks();

    this.destroy.onDestroy(() => clearTimeout(this.closeTimer));
  }
  loadTasks(cursor: string | null = null): void {
    this.tasksApi
      .list(cursor)
      .pipe(takeUntilDestroyed(this.destroy))
      .subscribe({
        next: (page) => {
          this.remote.set(cursor ? [...this.remote(), ...page.tasks] : page.tasks);

          this.taskCursor.set(page.cursor);

          this.remoteError.set('');
        },
        error: () =>
          this.remoteError.set(
            'Saved plan tasks could not be loaded. Local work items are still available.',
          ),
      });
  }

  readonly items = computed(() =>
    [...this.store.items(), ...this.remote()].filter(
      (item) => item.project === this.selectedProject(),
    ),
  );
  @ViewChild('projectDialog') projectDialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('switchDialog') switchDialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('itemDialog') itemDialog!: ElementRef<HTMLDialogElement>;
  projectName = '';
  projectDescription = '';
  switchName = '';
  editing: string | undefined;
  draft: WorkItemDraft = {
    title: '',
    description: '',
    project: '',
    status: 'pending',
    priority: 'Medium',
    due: '',
  };
  hasProject(name: string): boolean {
    return this.catalog.projects().some((project) => project.name === name);
  }
  inStatus(status: WorkStatus) {
    return this.items().filter((item) => item.status === status);
  }
  createProject(): void {
    this.projectName = this.itemDialog?.nativeElement.open ? this.draft.project : '';

    this.projectDescription = '';

    this.projectDialog.nativeElement.showModal();
  }
  async saveProject(): Promise<void> {
    if (await this.catalog.create(this.projectName, this.projectDescription)) {
      this.draft.project = this.catalog.selected();

      this.remoteProject.set('');

      this.projectDialog.nativeElement.close();
    }
  }
  switchProject(): void {
    this.switchName = this.selectedProject();

    this.switchDialog.nativeElement.showModal();
  }
  saveSelection(): void {
    if (this.allProjects().includes(this.switchName)) {
      this.remoteProject.set(this.switchName);

      this.switchDialog.nativeElement.close();
    }
  }
  openItem(item?: WorkItem, status: WorkStatus = 'pending'): void {
    this.closing.set(false);

    clearTimeout(this.closeTimer);

    this.activeTask = item && 'owner' in item ? (item as ServerTask) : null;

    this.remoteError.set('');

    this.busy.set(false);

    this.editing = item?.id;

    this.draft = item
      ? { ...item, priority: item.priority ?? 'Medium', due: item.due ?? '' }
      : {
          title: '',
          description: '',
          project: this.selectedProject(),
          assigned_to: this.auth.identity()?.displayName ?? '',
          created_at: new Date().toISOString(),
          status,
          priority: 'Medium',
          due: '',
        };

    this.itemDialog.nativeElement.showModal();

    if (this.activeTask) {
      const active = this.activeTask;

      this.busy.set(true);

      this.tasksApi
        .read(active)
        .pipe(takeUntilDestroyed(this.destroy))
        .subscribe({
          next: (result) => {
            if (this.activeTask !== active) {
              return;
            }

            this.activeTask = result.task;

            this.draft = { ...result.task };

            this.busy.set(false);
          },
          error: () => {
            this.remoteError.set('Task details could not be opened. Close and try again.');

            this.busy.set(true);
          },
        });
    }
  }
  saveItem(): void {
    if (this.busy() || this.closing()) {
      return;
    }

    if (!this.catalog.projects().some((project) => project.name === this.draft.project)) {
      this.remoteError.set('Select a saved project. Create or import a project first.');

      return;
    }

    if (this.activeTask) {
      if (!this.activeTask.editable) {
        return;
      }

      this.busy.set(true);
      const task = this.activeTask;

      this.tasksApi
        .save(task, this.draft)
        .pipe(takeUntilDestroyed(this.destroy))
        .subscribe({
          next: (result) => {
            this.remote.update((tasks) =>
              tasks.map((t) => (t.id === task.id && t.owner === task.owner ? result.task : t)),
            );

            this.busy.set(false);

            this.closeItem();
          },
          error: () => {
            this.busy.set(false);

            this.remoteError.set(
              'Task could not be saved. Reload it before retrying; another editor may have changed it.',
            );
          },
        });

      return;
    }

    if (this.store.save(this.draft, this.editing)) {
      this.remoteProject.set(this.draft.project.trim());

      this.closeItem();
    }
  }
  deleteItem(): void {
    if (this.editing && this.store.remove(this.editing)) {
      this.closeItem();
    }
  }
  drag(event: DragEvent, id: string): void {
    event.dataTransfer?.setData('text/plain', id);
  }
  drop(event: DragEvent, status: WorkStatus): void {
    event.preventDefault();
    const id = event.dataTransfer?.getData('text/plain');

    if (
      id &&
      this.items().some((item) => item.id === id) &&
      !this.remote().some((task) => task.id === id)
    ) {
      this.store.transition(id, status);
    }
  }
}
