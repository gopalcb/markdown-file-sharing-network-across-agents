import { Component, DestroyRef, inject, signal } from '@angular/core';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { WoCardComponent, WoTextInputComponent } from '../../../theme/components';
import { WorkStatus } from '../../interfaces/work-item.interface';
import {
  ProjectTrackerStoreService,
  WORK_STATUSES,
} from '../../services/project-tracker-store.service';
import { MarkdownComponent } from '../markdown/markdown.component';

@Component({
  imports: [
    RouterLink,
    ReactiveFormsModule,
    WoCardComponent,
    WoTextInputComponent,
    MarkdownComponent,
  ],
  templateUrl: './work-item.component.html',
})
export class WorkItemComponent {
  readonly store = inject(ProjectTrackerStoreService);
  private readonly router = inject(Router);
  readonly statuses = WORK_STATUSES;
  readonly preview = signal(false);
  readonly missing = signal(false);
  readonly loadError = signal('');
  id: string | null = null;
  submitted = false;
  readonly form = new FormGroup({
    title: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(/\S/), Validators.maxLength(120)],
    }),
    project: new FormControl('', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(/\S/), Validators.maxLength(80)],
    }),
    status: new FormControl<WorkStatus>('pending', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(/^(pending|in-progress|done)$/)],
    }),
    description: new FormControl('', {
      nonNullable: true,
      validators: [Validators.maxLength(20000)],
    }),
  });
  constructor() {
    // Load synchronously so direct links can find saved items before Angular effects run.
    this.store.reload();

    this.loadError.set(this.store.error());
    const subscription = inject(ActivatedRoute).paramMap.subscribe((params) => {
      this.id = params.get('id');

      this.submitted = false;

      this.preview.set(false);

      this.populateCurrentItem();
    });

    inject(DestroyRef).onDestroy(() => subscription.unsubscribe());
  }
  private populateCurrentItem(): void {
    this.missing.set(false);

    if (this.loadError()) {
      return;
    }
    const item = this.store.items().find((entry) => entry.id === this.id);

    this.missing.set(!!this.id && !item);

    this.form.reset(item ?? { title: '', project: '', status: 'pending', description: '' });
  }
  retryLoad(): void {
    // This action is only offered for initial read failures, never save conflicts.
    if (!this.loadError()) {
      return;
    }

    this.store.reload();

    this.loadError.set(this.store.error());

    this.populateCurrentItem();
  }
  moveDescriptionTab(event: KeyboardEvent): void {
    let next: boolean;

    if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
      next = !this.preview();
    } else if (event.key === 'Home') {
      next = false;
    } else if (event.key === 'End') {
      next = true;
    } else {
      return;
    }

    event.preventDefault();

    this.preview.set(next);

    document.getElementById(next ? 'description-preview' : 'description-edit')?.focus();
  }
  save(): void {
    this.submitted = true;

    this.form.markAllAsTouched();

    if (this.form.invalid) {
      const name = Object.entries(this.form.controls).find(([, control]) => control.invalid)?.[0];

      document.getElementById('work-' + name)?.focus();

      return;
    }
    const item = this.store.save(this.form.getRawValue(), this.id ?? undefined);

    if (item) {
      void this.router.navigate(['/work-organizer/workspace/project-tracker']);
    }
  }
}
