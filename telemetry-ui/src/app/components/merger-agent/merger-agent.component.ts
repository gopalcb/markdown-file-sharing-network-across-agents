import {
  Component,
  DestroyRef,
  ElementRef,
  ViewChild,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Subscription } from 'rxjs';
import { WoSpinnerComponent } from '../../../theme/components';
import { DocumentSummary } from '../../interfaces/document.interface';
import { DocumentsService } from '../../services/documents.service';
import { MarkdownComponent } from '../markdown/markdown.component';

type MergerTab = 'documents' | 'merged';

interface MergedDocument {
  id: string;
  filename: string;
  content: string;
  createdAt: string;
  sources: string[];
}

interface DocumentPreview {
  filename: string;
  content: string;
}

@Component({
  imports: [MarkdownComponent, WoSpinnerComponent],
  templateUrl: './merger-agent.component.html',
})
export class MergerAgentComponent {
  private readonly documentsApi = inject(DocumentsService);
  private listRequest?: Subscription;
  private readRequest?: Subscription;
  private previewTrigger?: HTMLElement;
  readonly documents = signal<DocumentSummary[]>([]);
  readonly mergedDocuments = signal<MergedDocument[]>([]);
  readonly selectedKeys = signal<ReadonlySet<string>>(new Set());
  readonly activeTab = signal<MergerTab>('documents');
  readonly instructions = signal('');
  readonly loading = signal(false);
  readonly reading = signal(false);
  readonly listError = signal('');
  readonly previewError = signal('');
  readonly preview = signal<DocumentPreview | null>(null);
  readonly notice = signal('');
  readonly cursor = signal<string | null>(null);
  readonly selectedDocuments = computed(() => {
    const selectedKeys = this.selectedKeys();

    return this.documents().filter((document) => selectedKeys.has(this.documentKey(document)));
  });
  readonly canMerge = computed(
    () => this.selectedDocuments().length > 0 && this.instructions().trim().length > 0,
  );
  @ViewChild('previewDialog') previewDialog!: ElementRef<HTMLDialogElement>;

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.listRequest?.unsubscribe();

      this.readRequest?.unsubscribe();
    });

    this.loadDocuments();
  }

  documentKey(document: DocumentSummary): string {
    return document.owner + '/' + document.file_id;
  }

  selectTab(tab: MergerTab): void {
    this.activeTab.set(tab);

    requestAnimationFrame(() => document.getElementById('merger-' + tab + '-tab')?.focus());
  }

  moveTab(event: KeyboardEvent, tab: MergerTab): void {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) {
      return;
    }

    event.preventDefault();

    this.selectTab(tab);
  }

  toggleDocument(document: DocumentSummary, checked: boolean): void {
    const next = new Set(this.selectedKeys());
    const key = this.documentKey(document);

    if (checked) {
      next.add(key);
    } else {
      next.delete(key);
    }

    this.selectedKeys.set(next);

    this.notice.set('');
  }

  loadDocuments(cursor: string | null = null): void {
    this.listRequest?.unsubscribe();

    this.loading.set(true);

    this.listError.set('');

    this.listRequest = this.documentsApi.list('all', cursor).subscribe({
      next: (page) => {
        const existing = cursor ? this.documents() : [];
        const unique = new Map(existing.map((document) => [this.documentKey(document), document]));

        for (const document of page.documents) {
          unique.set(this.documentKey(document), document);
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

  openDocument(document: DocumentSummary, trigger: EventTarget | null): void {
    this.previewTrigger = trigger as HTMLElement;

    this.readRequest?.unsubscribe();

    this.preview.set({ filename: document.filename, content: '' });

    this.previewError.set('');

    this.reading.set(true);

    this.previewDialog.nativeElement.showModal();

    this.readRequest = this.documentsApi.read(document, 'all').subscribe({
      next: (result) => {
        this.preview.set({ filename: result.document.filename, content: result.document.content });

        this.reading.set(false);
      },
      error: () => {
        this.previewError.set('This document could not be opened. Please try again.');

        this.reading.set(false);
      },
    });
  }

  openMergedDocument(document: MergedDocument, trigger: EventTarget | null): void {
    this.previewTrigger = trigger as HTMLElement;

    this.previewError.set('');

    this.reading.set(false);

    this.preview.set({ filename: document.filename, content: document.content });

    this.previewDialog.nativeElement.showModal();
  }

  closePreview(): void {
    this.readRequest?.unsubscribe();

    this.previewDialog.nativeElement.close();

    this.previewTrigger?.focus();
  }

  mergeDocuments(): void {
    if (!this.canMerge()) {
      return;
    }

    const sources = this.selectedDocuments().map((document) => document.filename);
    const sequence = this.mergedDocuments().length + 1;
    const filename = `merged-document-${sequence}.md`;
    const content = [
      '# Merged document',
      '',
      this.instructions().trim(),
      '',
      '## Source documents',
      '',
      ...sources.map((source) => '- ' + source),
    ].join('\n');

    this.mergedDocuments.update((documents) => [
      {
        id: crypto.randomUUID(),
        filename,
        content,
        createdAt: new Date().toISOString(),
        sources,
      },
      ...documents,
    ]);

    this.instructions.set('');

    this.selectedKeys.set(new Set());

    this.notice.set(`${filename} was added to this UI preview.`);

    this.selectTab('merged');
  }
}
