export type CrawlStats = {
  queued: number;
  leased: number;
  done: number;
  failed: number;
};

export type CrawlStatus = {
  crawl_id: string;
  lifecycle: "active" | "paused" | "cancelled" | "completed" | string;
  result_count: number;
  stats: CrawlStats;
  completed: boolean;
};

export type CrawlListPage = {
  total: number;
  limit: number;
  offset: number;
  items: CrawlStatus[];
};

export type CrawlResultsPage = {
  crawl_id: string;
  total: number;
  limit: number;
  offset: number;
  items: PageRecord[];
};

export type PageRecord = {
  url: string;
  status_code: number | null;
  content_type: string | null;
  title: string | null;
  links: string[];
  engine: string;
  route: string;
  metadata: Record<string, unknown>;
};

export type Health = {
  ok: boolean;
  postgres: boolean;
  redis: boolean;
};

export type SubmitCrawl = {
  urls: string[];
  crawl_id?: string;
  follow_links?: boolean;
  max_pages?: number;
};

export type ApiOptions = {
  baseUrl?: string;
  token?: string;
  fetchImpl?: typeof fetch;
};

export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function envBaseUrl(): string {
  const explicit = process.env.SYNCRAWLER_TUI_API_URL ?? process.env.SYNCRAWLER_API_URL;
  if (explicit) return explicit.replace(/\/$/, "");

  let host = process.env.SYNCRAWLER_API_BIND ?? "127.0.0.1";
  if (host === "0.0.0.0" || host === "::") host = "127.0.0.1";
  const port = process.env.SYNCRAWLER_API_PORT ?? "8080";
  return `http://${host}:${port}`;
}

export class SyndCrawlerApi {
  readonly baseUrl: string;
  private readonly token: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiOptions = {}) {
    this.baseUrl = (options.baseUrl ?? envBaseUrl()).replace(/\/$/, "");
    this.token = options.token ?? process.env.SYNCRAWLER_API_TOKEN ?? "";
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  hasToken(): boolean {
    return this.token.length > 0;
  }

  health(): Promise<Health> {
    return this.request<Health>("/healthz", {}, false);
  }

  listCrawls(limit = 50, offset = 0): Promise<CrawlListPage> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return this.request<CrawlListPage>(`/v1/crawls?${params}`);
  }

  status(crawlId: string): Promise<CrawlStatus> {
    return this.request<CrawlStatus>(`/v1/crawls/${encodeURIComponent(crawlId)}`);
  }

  results(crawlId: string, limit = 100, offset = 0): Promise<CrawlResultsPage> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    return this.request<CrawlResultsPage>(
      `/v1/crawls/${encodeURIComponent(crawlId)}/results?${params}`,
    );
  }

  submit(payload: SubmitCrawl): Promise<CrawlStatus> {
    return this.request<CrawlStatus>("/v1/crawls", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  pause(crawlId: string): Promise<CrawlStatus> {
    return this.request<CrawlStatus>(`/v1/crawls/${encodeURIComponent(crawlId)}/pause`, {
      method: "POST",
    });
  }

  resume(crawlId: string): Promise<CrawlStatus> {
    return this.request<CrawlStatus>(`/v1/crawls/${encodeURIComponent(crawlId)}/resume`, {
      method: "POST",
    });
  }

  cancel(crawlId: string): Promise<CrawlStatus> {
    return this.request<CrawlStatus>(`/v1/crawls/${encodeURIComponent(crawlId)}`, {
      method: "DELETE",
    });
  }

  private async request<T>(
    path: string,
    init: RequestInit = {},
    authenticated = true,
  ): Promise<T> {
    if (authenticated && !this.token) {
      throw new ApiError("SYNCRAWLER_API_TOKEN is not set", 401);
    }

    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    if (authenticated) headers.set("Authorization", `Bearer ${this.token}`);

    const response = await this.fetchImpl(`${this.baseUrl}${path}`, {
      ...init,
      headers,
    });

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`.trim();
      try {
        const body = (await response.json()) as { detail?: unknown };
        if (typeof body.detail === "string") detail = body.detail;
      } catch {
        // Keep the HTTP status when a non-JSON error body is returned.
      }
      throw new ApiError(detail, response.status);
    }

    return (await response.json()) as T;
  }
}
