import { DatePipe } from '@angular/common';
import { Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { Subscription } from 'rxjs';
import { WoCardComponent, WoSpinnerComponent } from '../../../theme/components';
import { PortfolioEvent } from '../../interfaces/portfolio-analytics.interface';
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

  constructor() {
    inject(DestroyRef).onDestroy(() => this.request?.unsubscribe());

    this.refresh();
  }

  refresh(): void {
    this.request?.unsubscribe();

    this.collected = new Map();

    this.receivedCount.set(0);

    this.pageIndex.set(0);

    this.load(null, 0);
  }

  continueLoading(): void {
    this.load(this.nextCursor(), 0);
  }

  retry(): void {
    this.load(this.requestedCursor, 0);
  }

  search(value: string): void {
    this.query.set(value);

    this.pageIndex.set(0);
  }

  movePage(change: number): void {
    this.pageIndex.update((page) => Math.min(this.pageCount() - 1, Math.max(0, page + change)));
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
