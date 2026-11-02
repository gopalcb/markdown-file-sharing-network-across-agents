export type WorkStatus = 'pending' | 'in-progress' | 'done';

export interface WorkItemDraft {
  title: string;
  project: string;
  description: string;
  status: WorkStatus;
  priority?: 'High' | 'Medium' | 'Low';
  due?: string;
  assigned_to?: string;
  created_at?: string;
}

export interface WorkItem extends WorkItemDraft {
  id: string;
}
