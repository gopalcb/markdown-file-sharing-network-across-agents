import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { timeout } from 'rxjs';
import {
  PortfolioActivity,
  PortfolioDeleteResult,
  PortfolioEvent,
  PortfolioEventIdentity,
  PortfolioEventPage,
} from '../interfaces/portfolio-analytics.interface';

const VISITOR_COLOURS = ['#edf5f2', '#eef2fb', '#fbf3e6', '#f4eef8', '#eaf5f6', '#f9edf0'];

/** Keep each anonymous session label unchanged while more report pages arrive. */
export class VisitorLabels {
  private readonly names = new Map<string, string>();
  private readonly used = new Set<number>();

  name(key: string): string {
    const existing = this.names.get(key);

    if (existing) {
      return existing;
    }

    let number = 10000 + (parseInt(key.slice(0, 12), 16) % 90000);

    if (!Number.isFinite(number)) {
      number = 10000;
    }

    while (this.used.has(number) && this.used.size < 90000) {
      number = 10000 + ((number - 10000 + 1) % 90000);
    }

    if (this.used.size >= 90000) {
      throw new Error('The five-digit visitor label range is full.');
    }

    const label = `Visitor-${number}`;

    this.names.set(key, label);

    this.used.add(number);

    return label;
  }

  colour(key: string): string {
    const number = parseInt(key.slice(-8), 16) || 0;

    return VISITOR_COLOURS[number % VISITOR_COLOURS.length];
  }
}

/** Event UUIDs are unique within a visitor session, matching the storage key. */
export function portfolioEventKey(event: PortfolioEventIdentity): string {
  return `${event.visitor_key}:${event.event_id}`;
}

function eventTime(event: PortfolioEvent): number {
  const client = Date.parse(event.client_timestamp);

  return Number.isFinite(client) ? client : Date.parse(event.visited_at);
}

function isView(event: PortfolioEvent): boolean {
  return event.event_type === 'project_open' || event.event_type === 'page_view';
}

function matchesDestination(click: PortfolioEvent, view: PortfolioEvent): boolean {
  const element = click.metadata.element_id ?? '';
  const destination = (view.project || view.page_title).trim().toLowerCase();

  if (element === 'projects-menu') {
    return view.event_type === 'project_open' && destination === 'runtime controller';
  }

  if (element === 'about-menu') {
    return destination === 'about me';
  }

  return element.startsWith('content-') && click.target.trim().toLowerCase() === destination;
}

function result(event: PortfolioEvent): string {
  if (event.status === 'served') {
    return 'Loaded';
  }

  if (event.status === 'logged') {
    return isView(event) ? 'View recorded' : 'Recorded';
  }

  if (event.status === 'received') {
    return 'Incomplete';
  }

  return 'Failed';
}

function pageName(event: PortfolioEvent): string {
  return event.project || event.page_title || event.page_path || 'Portfolio';
}

function activity(event: PortfolioEvent): string {
  const labels: Record<string, string> = {
    element_clicked: 'Control clicked',
    project_open: 'Project page load',
    page_view: 'Page load',
    video_play: 'Video play',
    video_pause: 'Video pause',
    video_ended: 'Video ended',
  };

  return labels[event.event_type] ?? event.event_type.replaceAll('_', ' ');
}

function remark(event: PortfolioEvent, click?: PortfolioEvent): string {
  const page = pageName(event);

  if (click) {
    const outcome =
      event.status === 'served'
        ? 'page loaded successfully'
        : event.status === 'received'
          ? 'page load outcome was not completed'
          : event.status === 'logged'
            ? 'page view recorded; load result unavailable'
            : `page load failed (${event.status.replaceAll('_', ' ')})`;

    return `Visitor clicked ${click.target || page}; ${outcome}.`;
  }

  if (event.event_type === 'element_clicked') {
    return `Visitor clicked ${event.target || 'a control'} on ${page}.`;
  }

  if (isView(event)) {
    return event.status === 'served'
      ? `${page} loaded successfully; no matching menu click recorded.`
      : event.status === 'logged'
        ? `${page} view recorded; load result unavailable.`
        : `${page}: ${result(event).toLowerCase()} (${event.status.replaceAll('_', ' ')}).`;
  }

  if (event.event_type.startsWith('video_')) {
    const seconds = event.metadata.current_time_seconds;
    const position = typeof seconds === 'number' ? ` at ${Math.round(seconds)} seconds` : '';

    return `${activity(event)}: ${event.metadata.video_title || page}${position}.`;
  }

  return `${activity(event)} on ${page}.`;
}

/** Match only one real menu click to one destination view within ten client-clock seconds. */
export function portfolioActivities(
  events: PortfolioEvent[],
  labels = new VisitorLabels(),
): PortfolioActivity[] {
  const unique = [...new Map(events.map((event) => [portfolioEventKey(event), event])).values()];
  const chronological = unique.sort(
    (left, right) =>
      eventTime(left) - eventTime(right) || left.event_id.localeCompare(right.event_id),
  );
  const visitors = new Map<string, PortfolioEvent[]>();

  for (const event of chronological) {
    const group = visitors.get(event.visitor_key) ?? [];

    group.push(event);

    visitors.set(event.visitor_key, group);
  }

  const pairedClicks = new Set<string>();
  const clickForView = new Map<string, PortfolioEvent>();

  for (const group of visitors.values()) {
    for (const view of group.filter(isView)) {
      const click = group
        .filter((candidate) => {
          const difference = eventTime(view) - eventTime(candidate);

          return (
            candidate.event_type === 'element_clicked' &&
            !pairedClicks.has(portfolioEventKey(candidate)) &&
            difference >= 0 &&
            difference <= 10000 &&
            matchesDestination(candidate, view)
          );
        })
        .at(-1);

      if (click) {
        pairedClicks.add(portfolioEventKey(click));

        clickForView.set(portfolioEventKey(view), click);
      }
    }
  }

  return chronological
    .filter((event) => !pairedClicks.has(portfolioEventKey(event)))
    .map((event) => {
      const click = clickForView.get(portfolioEventKey(event));
      const geo = event.location;

      return {
        id: portfolioEventKey(event),
        visitorKey: event.visitor_key,
        visitor: labels.name(event.visitor_key),
        colour: labels.colour(event.visitor_key),
        visitedAt: click?.visited_at ?? event.visited_at,
        location: [geo.city, geo.region, geo.country].filter(Boolean).join(', ') || 'Unavailable',
        activity: click ? 'Menu click + page load' : activity(event),
        page: pageName(event),
        result: result(event),
        remark: remark(event, click),
        language: event.metadata.language || '—',
        referrer: event.metadata.referrer_host || 'Direct / unavailable',
        eventCount: click ? 2 : 1,
        events: (click ? [click, event] : [event]).map(({ visitor_key, event_id }) => ({
          visitor_key,
          event_id,
        })),
      };
    })
    .sort(
      (left, right) =>
        Date.parse(right.visitedAt) - Date.parse(left.visitedAt) || left.id.localeCompare(right.id),
    );
}

@Injectable({ providedIn: 'root' })
export class PortfolioAnalyticsService {
  private readonly http = inject(HttpClient);

  delete(events: PortfolioEventIdentity[]) {
    return this.http
      .delete<PortfolioDeleteResult>('/api/analytics/gh-portfolio', { body: { events } })
      .pipe(timeout(15000));
  }

  page(cursor: string | null = null) {
    const params = cursor ? new HttpParams().set('cursor', cursor) : new HttpParams();

    return this.http
      .get<PortfolioEventPage>('/api/analytics/gh-portfolio', { params })
      .pipe(timeout(15000));
  }
}
