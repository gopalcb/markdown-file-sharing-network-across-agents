import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { WorkItem, WorkItemDraft } from '../interfaces/work-item.interface';
export interface ServerTask extends WorkItem {
  owner: string;
  revision: number;
  editable: boolean;
}
@Injectable({ providedIn: 'root' })
export class TasksService {
  private readonly http = inject(HttpClient);
  list(cursor: string | null = null) {
    let params = new HttpParams().set('scope', 'all');

    if (cursor) {
      params = params.set('cursor', cursor);
    }

    return this.http.get<{ tasks: ServerTask[]; cursor: string | null }>('/api/tasks', { params });
  }
  read(task: ServerTask) {
    return this.http.get<{ task: ServerTask }>(this.path(task));
  }
  save(task: ServerTask, draft: WorkItemDraft) {
    const { title, project, description, status, priority, due, assigned_to } = draft;

    return this.http.patch<{ task: ServerTask }>(this.path(task), {
      expected_revision: task.revision,
      title,
      project,
      description,
      status,
      priority,
      due,
      assigned_to,
    });
  }
  private path(task: ServerTask) {
    return '/api/tasks/' + encodeURIComponent(task.owner) + '/' + encodeURIComponent(task.id);
  }
}
