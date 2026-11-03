export interface PortfolioEvent {
  event_id: string;
  visitor_key: string;
  visited_at: string;
  client_timestamp: string;
  event_type: string;
  target: string;
  project: string;
  page_title: string;
  page_path: string;
  status: string;
  location: { city: string; region: string; country: string; timezone: string };
  metadata: {
    element_id?: string;
    video_title?: string;
    current_time_seconds?: number;
    language?: string;
    referrer_host?: string;
    browser_timezone?: string;
  };
}

export interface PortfolioEventPage {
  events: PortfolioEvent[];
  next_cursor: string | null;
  as_of: string;
}

export interface PortfolioActivity {
  id: string;
  visitorKey: string;
  visitor: string;
  colour: string;
  visitedAt: string;
  location: string;
  activity: string;
  page: string;
  result: string;
  remark: string;
  language: string;
  referrer: string;
  eventCount: number;
}
