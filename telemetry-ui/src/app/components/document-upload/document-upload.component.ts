import {
  Component,
  DestroyRef,
  ElementRef,
  EventEmitter,
  Output,
  ViewChild,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { DocumentsService } from '../../services/documents.service';
import { AuthService } from '../../services/auth.service';
import { UploadFile, UploadReceipt } from '../../interfaces/document.interface';
import { SelectComponent } from '../select/select.component';

@Component({
  selector: 'wo-document-upload',
  imports: [FormsModule, SelectComponent],
  templateUrl: './document-upload.component.html',
})
export class DocumentUploadComponent {
  private readonly api = inject(DocumentsService);
  private readonly auth = inject(AuthService);
  private readonly destroy = inject(DestroyRef);
  @ViewChild('dialog') dialog!: ElementRef<HTMLDialogElement>;
  @Output() uploaded = new EventEmitter<void>();
  readonly busy = signal(false);
  readonly reading = signal(false);
  readonly error = signal('');
  readonly folders = signal<{ name: string; prefix: string }[]>([]);
  readonly owner = signal('');
  readonly cursor = signal<string | null>(null);
  readonly receipts = signal<UploadReceipt[]>([]);
  readonly files = signal<UploadFile[]>([]);
  readonly submitted = signal(false);
  readonly folderLoading = signal(false);
  group = 'workflow-docs';
  kind = 'document';
  project = '';
  assignee = '';
  title = '';
  category = '';
  readonly kinds = [
    { value: 'document', label: 'Document' },
    { value: 'plan', label: 'Plan — create linked task' },
  ];
  get options() {
    return this.folders().map((folder) => ({ value: folder.name, label: folder.name }));
  }
  get prefix() {
    return this.folders().find((f) => f.name === this.group)?.prefix ?? '';
  }
  open(): void {
    this.error.set('');

    this.receipts.set([]);

    this.files.set([]);

    this.submitted.set(false);

    this.assignee = this.auth.identity()?.displayName ?? '';

    this.dialog.nativeElement.showModal();

    this.loadFolders();
  }
  loadFolders(cursor: string | null = null): void {
    this.folderLoading.set(true);

    this.api
      .folders(cursor)
      .pipe(takeUntilDestroyed(this.destroy))
      .subscribe({
        next: (page) => {
          this.owner.set(page.owner);

          this.folders.set([
            ...new Map(
              (cursor ? [...this.folders(), ...page.folders] : page.folders).map((folder) => [
                folder.name,
                folder,
              ]),
            ).values(),
          ]);

          this.cursor.set(page.cursor);

          this.folderLoading.set(false);
        },
        error: () => {
          this.folderLoading.set(false);

          this.error.set('Upload folders could not be loaded. Please retry.');
        },
      });
  }
  async choose(event: Event): Promise<void> {
    if (this.busy() || this.submitted()) {
      return;
    }
    const files = Array.from((event.target as HTMLInputElement).files ?? []);

    this.files.set([]);

    this.error.set('');

    this.reading.set(true);

    try {
      if (!files.length || files.length > 10) {
        throw new Error('Choose between 1 and 10 Markdown files.');
      }

      if (new Set(files.map((file) => file.name)).size !== files.length) {
        throw new Error('Choose files with distinct names.');
      }
      const result: UploadFile[] = [];

      for (const file of files) {
        if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\.md$/.test(file.name) || file.name.includes('..')) {
          throw new Error(
            'Use safe .md filenames containing letters, numbers, dots, underscores or hyphens.',
          );
        }

        if (file.size > 262144) {
          throw new Error('Each file must be at most 256 KiB.');
        }
        const content = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());

        result.push({ name: file.name, content, metadata: {} });
      }

      this.files.set(result);
    } catch (error) {
      this.error.set(
        error instanceof Error ? error.message : 'Could not read these files as UTF-8 Markdown.',
      );
    } finally {
      this.reading.set(false);
    }
  }
  upload(): void {
    if (this.busy() || this.reading() || this.submitted() || !this.files().length || !this.prefix) {
      return;
    }
    const files = this.files().map((file) => ({
      ...file,
      metadata: {
        kind: this.kind,
        project: this.project.trim(),
        assigned_to: this.assignee.trim(),
        title: this.title.trim() || file.name.replace(/\.md$/, ''),
        category: this.category.trim(),
      },
    }));

    if (this.kind === 'plan' && (!this.project.trim() || !this.assignee.trim())) {
      this.error.set('Enter a project and assigned agent or human for the plan task.');

      return;
    }

    if (
      new TextEncoder().encode(JSON.stringify({ 'file-group': this.group, files })).length > 1048576
    ) {
      this.error.set('The batch exceeds 1 MiB. Choose fewer or smaller files.');

      return;
    }

    this.busy.set(true);

    this.submitted.set(true);

    this.error.set('');

    this.api
      .upload(this.group, files)
      .pipe(takeUntilDestroyed(this.destroy))
      .subscribe({
        next: (response) => {
          const valid =
            response.files?.length === files.length &&
            response.files.every(
              (receipt, index) =>
                receipt.name === files[index].name &&
                typeof receipt.file_id === 'string' &&
                ['READY', 'INCOMPLETE'].includes(receipt.status),
            );

          if (!valid) {
            this.error.set('Upload outcome is unknown. Check Documents before uploading again.');

            this.busy.set(false);

            return;
          }

          this.receipts.set(response.files);

          this.busy.set(false);

          if (response.files.some((file) => file.status === 'READY')) {
            this.uploaded.emit();
          }

          if (response.files.some((file) => file.status !== 'READY')) {
            this.error.set(
              'Some uploads are not confirmed. Keep the file IDs below for reconciliation; do not retry blindly.',
            );
          }
        },
        error: (error) => {
          this.busy.set(false);

          this.error.set(
            error.status === 400
              ? 'Upload rejected. Check the file limits and metadata. Close and reopen to correct the batch.'
              : 'Upload outcome is unknown. Check Documents before uploading again; the files may already have been stored.',
          );
        },
      });
  }
  close(): void {
    if (!this.busy() && !this.reading()) {
      this.dialog.nativeElement.close();
    }
  }
}
