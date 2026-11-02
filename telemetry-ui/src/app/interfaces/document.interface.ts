export interface DocumentSummary {
  owner: string;
  file_id: string;
  filename: string;
  category: string;
  created_at: string;
  size_bytes: number | null;
}

export interface DocumentPage {
  documents: DocumentSummary[];
  cursor: string | null;
}

export interface DocumentDetail {
  document: EditableDocument;
}

export interface EditableDocument extends DocumentSummary {
  size_bytes: number;
  content: string;
  sha256: string;
  etag: string;
  metadata: Record<string, string | boolean | number | null>;
  editable: boolean;
}
export interface UploadFile {
  name: string;
  content: string;
  metadata: Record<string, string | boolean | number | null>;
}
export interface UploadReceipt {
  name: string;
  file_id: string;
  status: 'READY' | 'INCOMPLETE';
  s3_key?: string;
  task?: unknown;
}
export interface DocumentFolders {
  owner: string;
  folders: { name: string; prefix: string }[];
  cursor: string | null;
}
