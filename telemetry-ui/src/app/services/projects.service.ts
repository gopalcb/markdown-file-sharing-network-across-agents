import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
export interface Project {
  id: string;
  name: string;
  description: string;
  created_at: string;
  owner: string;
}
@Injectable({ providedIn: 'root' })
export class ProjectsService {
  private readonly http = inject(HttpClient);
  list(cursor: string | null = null) {
    return this.http.get<{ projects: Project[]; cursor: string | null }>('/api/projects', {
      params: cursor ? { cursor } : {},
    });
  }
  create(name: string, description: string) {
    return this.http.post<{ project: Project }>('/api/projects', { name, description });
  }
}
