import {
  Component,
  DestroyRef,
  computed,
  inject,
  signal,
  ElementRef,
  ViewChild,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { WoSpinnerComponent } from '../../../theme/components';
import { FormsModule } from '@angular/forms';
import { DocumentUploadComponent } from '../document-upload/document-upload.component';
import { MarkdownEditorComponent } from '../markdown-editor/markdown-editor.component';
import { SelectComponent } from '../select/select.component';
import { DocumentSummary, EditableDocument } from '../../interfaces/document.interface';
import { DocumentsService } from '../../services/documents.service';
import { AuthService } from '../../services/auth.service';

@Component({
  selector: 'app-documents',
  imports: [
    WoSpinnerComponent,
    FormsModule,
    DocumentUploadComponent,
    MarkdownEditorComponent,
    SelectComponent,
  ],
  templateUrl: './documents.component.html',
})
export class DocumentsComponent {
  private readonly documentApi = inject(DocumentsService);
  private readonly auth = inject(AuthService);
  private listRequest?: Subscription;
  private readRequest?: Subscription;
  private saveRequest?: Subscription;
  readonly detail = signal<EditableDocument | null>(null);
  readonly saving = signal(false);
  readonly saveError = signal('');
  readonly saveNotice = signal('');
  readonly copySubmitted = signal(false);
  readonly savedCopy = signal<DocumentSummary | null>(null);
  readonly canRevise = computed(() =>
    Boolean(
      this.detail()?.editable &&
        this.detail()?.etag &&
        this.detail()?.owner === this.auth.identity()?.sub,
    ),
  );
  readonly dirty = computed(
    () => this.detail() !== null && this.content() !== this.detail()?.content,
  );
  readonly filterOptions = computed(() => [
    { value: 'all', label: 'All categories' },
    ...this.categories().map((category) => ({ value: 'category:' + category, label: category })),
    { value: 'mine', label: 'My documents' },
  ]);
  filterValue = 'all';
  @ViewChild('discardDialog') discardDialog!: ElementRef<HTMLDialogElement>;
  private pendingNavigation: (() => void) | null = null;
  guardNavigation(action: () => void): void {
    if (this.saving()) {
      return;
    }

    if (this.dirty()) {
      this.pendingNavigation = action;

      this.discardDialog.nativeElement.showModal();
    } else {
      action();
    }
  }
  discard(): void {
    this.discardDialog.nativeElement.close();
    const action = this.pendingNavigation;

    this.pendingNavigation = null;

    action?.();
  }
  selectDocument(document: DocumentSummary): void {
    this.guardNavigation(() => this.openDocument(document));
  }
  selectFilter(value: string): void {
    this.guardNavigation(() => {
      this.filterValue = value;

      this.filter(value);
    });
  }
  saveDocument(): void {
    const document = this.detail();

    if (!document || this.saving() || !this.dirty() || this.copySubmitted()) {
      return;
    }

    if (new TextEncoder().encode(this.content()).length > 262144) {
      this.saveError.set('Markdown must be at most 256 KiB.');

      return;
    }

    this.saving.set(true);

    this.saveError.set('');

    this.saveNotice.set('');

    if (!this.canRevise()) {
      this.saveEditedCopy(document);

      return;
    }

    this.saveRequest = this.documentApi.save(document, this.content(), document.etag).subscribe({
      next: (result) => {
        this.detail.set(result.document);

        this.content.set(result.document.content);

        this.saving.set(false);

        this.saveNotice.set('Saved to S3.');
      },
      error: (error) => {
        this.saving.set(false);

        this.saveError.set(
          error.status === 409
            ? 'This document changed. Your draft is preserved; reopen the document to review its latest version.'
            : 'Save was not confirmed. Your draft is preserved. Reopen the document to inspect its current version before retrying.',
        );
      },
    });
  }

  private saveEditedCopy(document: EditableDocument): void {
    const owner = this.auth.identity()?.sub;

    if (!owner) {
      this.saving.set(false);

      this.saveError.set('Sign in again before saving an edited copy. Your draft is preserved.');

      return;
    }

    this.copySubmitted.set(true);

    this.saveRequest = this.documentApi
      .upload(document.category, [
        {
          name: document.filename,
          content: this.content(),
          metadata: {
            ...Object.fromEntries(Object.entries(document.metadata ?? {}).slice(0, 48)),
            source_owner: document.owner,
            source_file_id: document.file_id,
          },
        },
      ])
      .subscribe({
        next: (response) => {
          const receipt = response.files?.[0];

          if (
            response.files?.length !== 1 ||
            receipt?.name !== document.filename ||
            receipt.status !== 'READY' ||
            !/^[0-9a-f]{8}-[0-9a-f]{4}-[45][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
              receipt.file_id,
            )
          ) {
            this.saving.set(false);

            this.saveError.set(
              'Edited copy was not confirmed. Your draft is preserved. Check Documents before uploading again.',
            );

            return;
          }

          const copy: DocumentSummary = {
            ...document,
            owner,
            file_id: receipt.file_id,
            created_at: new Date().toISOString(),
            size_bytes: new TextEncoder().encode(this.content()).length,
          };

          this.savedCopy.set(copy);

          this.openSavedCopy();
        },
        error: (error) => {
          this.saving.set(false);

          if ([400, 403, 413].includes(error.status)) {
            this.copySubmitted.set(false);

            this.saveError.set(
              'The edited copy was rejected. Your draft is preserved. Check the filename, category and file limits.',
            );
          } else {
            this.saveError.set(
              'Edited-copy upload outcome is unknown. Your draft is preserved. Check Documents before uploading again; the copy may already be stored.',
            );
          }
        },
      });
  }
  openSavedCopy(): void {
    const copy = this.savedCopy();

    if (!copy) {
      return;
    }

    this.saving.set(true);

    this.saveError.set('');

    this.readRequest?.unsubscribe();

    this.readRequest = this.documentApi.read(copy, 'mine').subscribe({
      next: (result) => {
        this.selected.set(result.document);

        this.detail.set(result.document);

        this.content.set(result.document.content);

        this.documents.update((documents) => [
          result.document,
          ...documents.filter(
            (item) =>
              item.owner !== result.document.owner || item.file_id !== result.document.file_id,
          ),
        ]);

        this.copySubmitted.set(false);

        this.savedCopy.set(null);

        this.saving.set(false);

        this.saveNotice.set('Edited copy saved to your S3 folder. The original is unchanged.');
      },
      error: () => {
        this.saving.set(false);

        this.saveError.set(
          'Your edited copy was saved, but could not be reopened. Your draft is preserved. Open the saved copy below; do not upload it again.',
        );
      },
    });
  }

  readonly scope = signal<'all' | 'mine'>('all');
  readonly documents = signal<DocumentSummary[]>([]);
  readonly query = signal('');
  readonly category = signal('');
  readonly categories = computed(() =>
    [...new Set(this.documents().map((document) => document.category))].sort(),
  );
  readonly filteredDocuments = computed(() => {
    const query = this.query().trim().toLowerCase();

    return this.documents().filter(
      (document) =>
        (!this.category() || document.category === this.category()) &&
        [document.filename, document.category, document.owner].some((value) =>
          value.toLowerCase().includes(query),
        ),
    );
  });
  filter(value: string): void {
    this.category.set(value.startsWith('category:') ? value.slice(9) : '');

    this.changeScope(value === 'mine' ? 'mine' : 'all');
  }
  readonly selected = signal<DocumentSummary | null>(null);
  readonly cursor = signal<string | null>(null);
  readonly loading = signal(false);
  readonly reading = signal(false);
  readonly content = signal('');
  readonly listError = signal('');
  readonly readError = signal('');
  retryCursor: string | null = null;

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.listRequest?.unsubscribe();

      this.readRequest?.unsubscribe();

      this.saveRequest?.unsubscribe();
    });

    this.loadList();
  }
  changeScope(scope: 'all' | 'mine'): void {
    if (this.scope() === scope) {
      return;
    }

    this.readRequest?.unsubscribe();

    this.scope.set(scope);

    this.selected.set(null);

    this.content.set('');

    this.detail.set(null);

    this.saveError.set('');

    this.saveNotice.set('');

    this.copySubmitted.set(false);

    this.savedCopy.set(null);

    this.readError.set('');

    this.reading.set(false);

    this.loadList();
  }
  loadList(cursor: string | null = null): void {
    this.listRequest?.unsubscribe();

    this.retryCursor = cursor;

    if (!cursor) {
      this.documents.set([]);

      this.cursor.set(null);
    }

    this.loading.set(true);

    this.listError.set('');

    this.listRequest = this.documentApi.list(this.scope(), cursor).subscribe({
      next: (page) => {
        const existing = cursor ? this.documents() : [];
        const unique = new Map(
          existing.map((document) => [document.owner + '/' + document.file_id, document]),
        );

        for (const document of page.documents) {
          unique.set(document.owner + '/' + document.file_id, document);
        }

        this.documents.set([...unique.values()]);

        this.cursor.set(page.cursor);

        this.loading.set(false);
      },
      error: () => {
        this.listError.set('Documents could not be loaded. Please try again.');

        this.loading.set(false);
      },
    });
  }
  openDocument(document: DocumentSummary): void {
    this.readRequest?.unsubscribe();

    this.selected.set(document);

    this.content.set('');

    this.detail.set(null);

    this.saveError.set('');

    this.saveNotice.set('');

    this.copySubmitted.set(false);

    this.savedCopy.set(null);

    this.readError.set('');

    this.reading.set(true);

    this.readRequest = this.documentApi.read(document, this.scope()).subscribe({
      next: (result) => {
        this.detail.set(result.document);

        this.content.set(result.document.content);

        this.reading.set(false);
      },
      error: () => {
        this.readError.set('This document could not be opened. Please try again.');

        this.reading.set(false);
      },
    });
  }
}
