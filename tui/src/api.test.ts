import { describe, expect, test } from "bun:test";
import { ApiError, SyndCrawlerApi } from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("SyndCrawlerApi", () => {
  test("lists crawls with bearer authentication", async () => {
    const requests: Request[] = [];
    const api = new SyndCrawlerApi({
      baseUrl: "http://127.0.0.1:8082/",
      token: "secret",
      fetchImpl: async (input, init) => {
        requests.push(new Request(input, init));
        return jsonResponse({ total: 1, limit: 50, offset: 0, items: [] });
      },
    });

    const result = await api.listCrawls();
    expect(result.total).toBe(1);
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe("http://127.0.0.1:8082/v1/crawls?limit=50&offset=0");
    expect(requests[0].headers.get("authorization")).toBe("Bearer secret");
  });

  test("submits a bounded crawl", async () => {
    let request: Request | undefined;
    const api = new SyndCrawlerApi({
      baseUrl: "http://crawler",
      token: "secret",
      fetchImpl: async (input, init) => {
        request = new Request(input, init);
        return jsonResponse({
          crawl_id: "catalog-test",
          lifecycle: "active",
          result_count: 0,
          stats: { queued: 1, leased: 0, done: 0, failed: 0 },
          completed: false,
        }, 202);
      },
    });

    const result = await api.submit({
      urls: ["https://example.com/"],
      crawl_id: "catalog-test",
      follow_links: true,
      max_pages: 25,
    });

    expect(result.crawl_id).toBe("catalog-test");
    expect(request?.method).toBe("POST");
    expect(await request?.json()).toEqual({
      urls: ["https://example.com/"],
      crawl_id: "catalog-test",
      follow_links: true,
      max_pages: 25,
    });
  });

  test("health does not require the bearer token", async () => {
    let request: Request | undefined;
    const api = new SyndCrawlerApi({
      baseUrl: "http://crawler",
      fetchImpl: async (input, init) => {
        request = new Request(input, init);
        return jsonResponse({ ok: true, postgres: true, redis: true });
      },
    });

    expect(await api.health()).toEqual({ ok: true, postgres: true, redis: true });
    expect(request?.headers.get("authorization")).toBeNull();
  });

  test("authenticated operations fail clearly without a token", async () => {
    const api = new SyndCrawlerApi({
      baseUrl: "http://crawler",
      fetchImpl: async () => jsonResponse({}),
    });

    await expect(api.listCrawls()).rejects.toEqual(
      new ApiError("SYNCRAWLER_API_TOKEN is not set", 401),
    );
  });

  test("surfaces API detail messages", async () => {
    const api = new SyndCrawlerApi({
      baseUrl: "http://crawler",
      token: "secret",
      fetchImpl: async () => jsonResponse({ detail: "crawl is cancelled" }, 409),
    });

    try {
      await api.resume("cancelled-crawl");
      throw new Error("expected request to fail");
    } catch (error) {
      expect(error).toBeInstanceOf(ApiError);
      expect((error as ApiError).status).toBe(409);
      expect((error as ApiError).message).toBe("crawl is cancelled");
    }
  });
});
