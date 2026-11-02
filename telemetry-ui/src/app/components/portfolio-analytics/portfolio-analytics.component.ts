import { DatePipe } from '@angular/common';
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
import { WoCardComponent, WoSpinnerComponent } from '../../../theme/components';
import {
  PortfolioEvent,
  PortfolioEventIdentity,
} from '../../interfaces/portfolio-analytics.interface';
import {
  PortfolioAnalyticsService,
  VisitorLabels,
  portfolioActivities,
  portfolioEventKey,
} from '../../services/portfolio-analytics.service';

@Component({
  selector: 'app-portfolio-analytics',
  imports: [DatePipe, WoCardComponent, WoSpinnerComponent],
  templateUrl: './portfolio-analytics.component.html',
})
export class PortfolioAnalyticsComponent {
  private readonly api = inject(PortfolioAnalyticsService);
  private readonly labels = new VisitorLabels();
  private request?: Subscription;
  private deletion?: Subscription;
  private pendingEvents: PortfolioEventIdentity[] = [];
  @ViewChild('deleteDialog') deleteDialog!: ElementRef<HTMLDialogElement>;
  @ViewChild('deleteButton') deleteButton!: ElementRef<HTMLButtonElement>;
  @ViewChild('searchInput') searchInput!: ElementRef<HTMLInputElement>;
  @ViewChild('reportHeading') reportHeading!: ElementRef<HTMLHeadingElement>;
  readonly selected = signal(new Set<string>());
  readonly deleting = signal(false);
  readonly deleteError = signal('');
  readonly reconcileRequired = signal(false);
  readonly notice = signal('');
  readonly pendingActivityCount = signal(0);
  readonly pendingRecordCount = signal(0);
  private collected = new Map<string, PortfolioEvent>();
  private requestedCursor: string | null = null;
  readonly events = signal<PortfolioEvent[]>([]);
  readonly loading = signal(false);
  readonly error = signal('');
  readonly nextCursor = signal<string | null>(null);
  readonly asOf = signal('');
  readonly receivedCount = signal(0);
  readonly query = signal('');
  readonly pageIndex = signal(0);
  readonly pageSize = 50;
  readonly timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  readonly activities = computed(() => portfolioActivities(this.events(), this.labels));
  readonly visitorCount = computed(
    () => new Set(this.events().map((event) => event.visitor_key)).size,
  );
  readonly filtered = computed(() => {
    const query = this.query().trim().toLowerCase();

    return this.activities().filter(
      (row) =>
        !query ||
        [
          row.visitor,
          row.location,
          row.activity,
          row.page,
          row.result,
          row.remark,
          row.language,
          row.referrer,
        ].some((value) => value.toLowerCase().includes(query)),
    );
  });
  readonly pageCount = computed(() =>
    Math.max(1, Math.ceil(this.filtered().length / this.pageSize)),
  );
  readonly displayed = computed(() =>
    this.filtered().slice(this.pageIndex() * this.pageSize, (this.pageIndex() + 1) * this.pageSize),
  );
  readonly allSelected = computed(
    () =>
      this.displayed().length > 0 && this.displayed().every((row) => this.selected().has(row.id)),
  );
  readonly someSelected = computed(() => this.selected().size > 0 && !this.allSelected());

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.request?.unsubscribe();

      this.deletion?.unsubscribe();
    });

    this.refresh();
  }

  refresh(): void {
    if (this.deleting()) {
      return;
    }

    this.selected.set(new Set());

    this.reconcileRequired.set(false);

    this.deleteError.set('');

    this.notice.set('');

    if (this.deleteDialog?.nativeElement.open) {
      this.deleteDialog.nativeElement.close();
    }

    this.request?.unsubscribe();

    this.collected = new Map();

    this.receivedCount.set(0);

    this.pageIndex.set(0);

    this.load(null, 0);
  }

  continueLoading(): void {
    if (this.loading() || this.deleting() || this.reconcileRequired()) {
      return;
    }

    this.selected.set(new Set());

    this.load(this.nextCursor(), 0);
  }

  retry(): void {
    if (this.loading() || this.deleting() || this.reconcileRequired()) {
      return;
    }

    this.selected.set(new Set());

    this.load(this.requestedCursor, 0);
  }

  search(value: string): void {
    if (this.deleting()) {
      return;
    }

    this.selected.set(new Set());

    this.query.set(value);

    this.pageIndex.set(0);
  }

  movePage(change: number): void {
    if (this.deleting()) {
      return;
    }

    this.selected.set(new Set());

    this.pageIndex.update((page) => Math.min(this.pageCount() - 1, Math.max(0, page + change)));
  }

  clearSearch(): void {
    this.search('');

    this.searchInput.nativeElement.focus();
  }

  selectRow(id: string, checked: boolean): void {
    if (this.loading() || this.deleting()) {
      return;
    }

    const selected = new Set(this.selected());

    checked ? selected.add(id) : selected.delete(id);

    this.selected.set(selected);
  }

  selectPage(checked: boolean): void {
    if (this.loading() || this.deleting()) {
      return;
    }

    this.selected.set(new Set(checked ? this.displayed().map((row) => row.id) : []));
  }

  requestDelete(): void {
    if (!this.selected().size || this.loading() || this.deleting() || this.reconcileRequired()) {
      return;
    }

    const rows = this.displayed().filter((row) => this.selected().has(row.id));

    this.pendingEvents = rows.flatMap((row) => row.events);

    this.pendingActivityCount.set(rows.length);

    this.pendingRecordCount.set(this.pendingEvents.length);

    this.deleteError.set('');

    this.deleteDialog.nativeElement.showModal();
  }

  closeDelete(completed = false): void {
    if (this.deleting()) {
      return;
    }

    this.deleteDialog.nativeElement.close();

    if (completed) {
      this.reportHeading.nativeElement.focus();
    } else {
      this.deleteButton.nativeElement.focus();
    }
  }

  confirmDelete(): void {
    if (this.deleting() || this.reconcileRequired() || !this.pendingEvents.length) {
      return;
    }

    this.deleting.set(true);

    this.deleteError.set('');

    this.notice.set('');
    const submitted = new Set(this.pendingEvents.map(portfolioEventKey));

    this.deletion = this.api.delete(this.pendingEvents).subscribe({
      next: (result) => {
        this.deleting.set(false);
        const removed = Array.isArray(result?.removed) ? result.removed : [];

        if (
          removed.some(
            (event) =>
              !event || typeof event.visitor_key !== 'string' || typeof event.event_id !== 'string',
          )
        ) {
          this.requireReconciliation(
            'Deletion was not confirmed. Refresh the report before trying again.',
          );

          return;
        }
        const confirmed = new Set(removed.map(portfolioEventKey));
        const valid =
          removed.length === confirmed.size &&
          confirmed.size === submitted.size &&
          [...confirmed].every((key) => submitted.has(key)) &&
          Number.isInteger(result?.deleted_count) &&
          result.deleted_count >= 0 &&
          Number.isInteger(result?.already_absent_count) &&
          result.already_absent_count >= 0 &&
          result.deleted_count + result.already_absent_count === submitted.size;

        if (!valid) {
          this.requireReconciliation(
            'Deletion was not confirmed. Refresh the report before trying again.',
          );

          return;
        }

        for (const key of confirmed) {
          this.collected.delete(key);
        }

        this.events.set([...this.collected.values()]);

        this.receivedCount.set(this.collected.size);

        this.pageIndex.set(Math.min(this.pageIndex(), this.pageCount() - 1));

        this.selected.set(new Set());

        this.pendingEvents = [];

        this.notice.set(
          `${result.deleted_count} ${result.deleted_count === 1 ? 'record' : 'records'} deleted${result.already_absent_count ? `; ${result.already_absent_count} already absent` : ''}.`,
        );

        this.closeDelete(true);
      },
      error: () => {
        this.deleting.set(false);

        this.requireReconciliation(
          'Deletion outcome is unknown. Refresh the report before trying again.',
        );
      },
    });
  }

  private requireReconciliation(message: string): void {
    this.reconcileRequired.set(true);

    this.deleteError.set(message);
  }

  private load(cursor: string | null, pages: number): void {
    this.requestedCursor = cursor;

    this.loading.set(true);

    this.error.set('');

    this.request = this.api.page(cursor).subscribe({
      next: (response) => {
        for (const event of response.events) {
          this.collected.set(portfolioEventKey(event), event);
        }

        this.receivedCount.set(this.collected.size);

        this.asOf.set(response.as_of);

        this.nextCursor.set(response.next_cursor);

        if (response.next_cursor && pages + 1 < 40) {
          this.load(response.next_cursor, pages + 1);

          return;
        }

        this.events.set([...this.collected.values()]);

        this.pageIndex.set(Math.min(this.pageIndex(), this.pageCount() - 1));

        this.loading.set(false);
      },
      error: () => {
        this.events.set([...this.collected.values()]);

        this.error.set(
          'Analytics could not be fully loaded. Retry to continue from the last successful page.',
        );

        this.loading.set(false);
      },
    });
  }
}
