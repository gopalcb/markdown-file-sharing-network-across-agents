import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import {
  DocumentDetail,
  DocumentPage,
  DocumentSummary,
  UploadFile,
  UploadReceipt,
  DocumentFolders,
} from '../interfaces/document.interface';

@Injectable({ providedIn: 'root' })
export class DocumentsService {
  private readonly http = inject(HttpClient);

  list(scope: 'all' | 'mine', cursor: string | null = null) {
    let params = new HttpParams().set('scope', scope);

    if (cursor) {
      params = params.set('cursor', cursor);
    }

    return this.http.get<DocumentPage>('/api/documents', { params });
  }

  folders(cursor: string | null = null) {
    return this.http.get<DocumentFolders>('/api/document-folders', {
      params: cursor ? { cursor } : {},
    });
  }
  upload(group: string, files: UploadFile[]) {
    return this.http.post<{ files: UploadReceipt[]; request_id: string }>('/api/documents', {
      'file-group': group,
      files,
    });
  }
  save(document: DocumentSummary, content: string, etag: string) {
    return this.http.put<DocumentDetail>(
      '/api/documents/' +
        encodeURIComponent(document.owner) +
        '/' +
        encodeURIComponent(document.file_id),
      { content, expected_etag: etag },
    );
  }
  read(document: DocumentSummary, scope: 'all' | 'mine') {
    return this.http.get<DocumentDetail>(
      '/api/documents/' +
        encodeURIComponent(document.owner) +
        '/' +
        encodeURIComponent(document.file_id),
      { params: { scope } },
    );
  }
}
