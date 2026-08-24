import { createCliRenderer } from "@opentui/core";
import {
  createRoot,
  useKeyboard,
  useTerminalDimensions,
} from "@opentui/react";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ApiError,
  type CrawlResultsPage,
  type CrawlStatus,
  type Health,
  SyndCrawlerApi,
} from "./api";
import {
  activeFrame,
  flowFrame,
  reducedMotionFromEnv,
  revealText,
} from "./motion";

type View = "dashboard" | "detail" | "new" | "help";

type NewCrawlForm = {
  url: string;
  crawlId: string;
  maxPages: string;
};

const palette = {
  bg: "#0b1020",
  panel: "#11182b",
  panelAlt: "#151f35",
  border: "#2b3a55",
  text: "#dbe7ff",
  muted: "#7e8da8",
  accent: "#6ea8fe",
  good: "#66d9a3",
  warn: "#f2c66d",
  bad: "#ff7d8a",
  purple: "#b59cff",
};

function safeError(error: unknown): string {
  if (error instanceof ApiError) return `${error.status}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

function lifecycleColor(lifecycle: string): string {
  if (lifecycle === "completed") return palette.good;
  if (lifecycle === "active") return palette.accent;
  if (lifecycle === "paused") return palette.warn;
  if (lifecycle === "cancelled") return palette.bad;
  return palette.muted;
}

function engineSummary(results: CrawlResultsPage | null): string {
  if (!results || results.items.length === 0) return "—";
  const http = results.items.filter((item) => item.engine === "http").length;
  const browser = results.items.filter((item) => item.engine === "browser").length;
  if (http > 0 && browser > 0) return `mixed ${http}/${browser}`;
  if (browser > 0) return `browser ${browser}`;
  return `http ${http}`;
}

function HealthPill({ label, ok }: { label: string; ok: boolean | undefined }) {
  const symbol = ok === undefined ? "○" : ok ? "●" : "×";
  const color = ok === undefined ? palette.muted : ok ? palette.good : palette.bad;
  return (
    <text>
      <span fg={color}>{symbol}</span>
      <span fg={palette.muted}> {label}</span>
    </text>
  );
}

function Header({
  health,
  apiUrl,
  motionTick,
  reducedMotion,
}: {
  health: Health | null;
  apiUrl: string;
  motionTick: number;
  reducedMotion: boolean;
}) {
  const title = revealText("SyndCrawler", motionTick + 1, reducedMotion);
  return (
    <box
      border
      borderStyle="rounded"
      borderColor={palette.border}
      backgroundColor={palette.panel}
      paddingLeft={1}
      paddingRight={1}
      height={5}
      flexDirection="column"
    >
      <box flexDirection="row" justifyContent="space-between">
        <text>
          <span fg={palette.accent}>◆</span>
          <strong> {title}</strong>
          <span fg={palette.muted}> operator console</span>
        </text>
        <text fg={palette.muted}>{apiUrl}</text>
      </box>
      <box flexDirection="row" gap={2}>
        <HealthPill label="API" ok={health?.ok} />
        <HealthPill label="Postgres" ok={health?.postgres} />
        <HealthPill label="Redis" ok={health?.redis} />
        <text>
          <span fg={palette.accent}>{flowFrame(motionTick, reducedMotion)}</span>
          <span fg={palette.muted}> fetch · parse · store</span>
        </text>
      </box>
    </box>
  );
}

function Dashboard({
  crawls,
  selectedIndex,
  narrow,
  loading,
  motionTick,
  reducedMotion,
}: {
  crawls: CrawlStatus[];
  selectedIndex: number;
  narrow: boolean;
  loading: boolean;
  motionTick: number;
  reducedMotion: boolean;
}) {
  return (
    <box
      title=" Crawls "
      titleColor={palette.accent}
      border
      borderStyle="rounded"
      borderColor={palette.border}
      backgroundColor={palette.panel}
      flexGrow={1}
      flexDirection="column"
      paddingLeft={1}
      paddingRight={1}
    >
      {crawls.length === 0 ? (
        <box flexGrow={1} alignItems="center" justifyContent="center" flexDirection="column">
          <text fg={palette.muted}>{loading ? "Loading crawls…" : "No crawls yet."}</text>
          {!loading && <text fg={palette.accent}>Press n to start one.</text>}
        </box>
      ) : (
        <>
          <box flexDirection="row" backgroundColor={palette.panelAlt} paddingLeft={1}>
            <text fg={palette.muted}>
              {narrow
                ? "   CRAWL                        STATE     DONE FAIL"
                : "   CRAWL                                 STATE       DONE  QUEUE LEASE FAIL"}
            </text>
          </box>
          {crawls.map((crawl, index) => {
            const selected = index === selectedIndex;
            const cursor = selected ? "›" : " ";
            const activity = crawl.lifecycle === "active"
              ? activeFrame(motionTick + index, reducedMotion)
              : "·";
            const idWidth = narrow ? 27 : 37;
            const id = crawl.crawl_id.length > idWidth
              ? `${crawl.crawl_id.slice(0, idWidth - 1)}…`
              : crawl.crawl_id.padEnd(idWidth);
            const state = crawl.lifecycle.toUpperCase().padEnd(narrow ? 9 : 11);
            const rowBg = selected ? "#1d2a45" : palette.panel;
            return (
              <box key={crawl.crawl_id} backgroundColor={rowBg} paddingLeft={1}>
                <text>
                  <span fg={selected ? palette.accent : palette.muted}>{cursor}</span>
                  <span fg={lifecycleColor(crawl.lifecycle)}>{activity} </span>
                  <span fg={selected ? palette.text : "#b8c5dc"}>{id} </span>
                  <span fg={lifecycleColor(crawl.lifecycle)}>{state}</span>
                  <span fg={palette.text}>{String(crawl.stats.done).padStart(5)}</span>
                  {!narrow && (
                    <>
                      <span fg={palette.muted}>{String(crawl.stats.queued).padStart(6)}</span>
                      <span fg={palette.purple}>{String(crawl.stats.leased).padStart(6)}</span>
                    </>
                  )}
                  <span fg={crawl.stats.failed > 0 ? palette.bad : palette.muted}>
                    {String(crawl.stats.failed).padStart(5)}
                  </span>
                </text>
              </box>
            );
          })}
        </>
      )}
    </box>
  );
}

function Detail({
  crawl,
  results,
  narrow,
  motionTick,
  reducedMotion,
}: {
  crawl: CrawlStatus | null;
  results: CrawlResultsPage | null;
  narrow: boolean;
  motionTick: number;
  reducedMotion: boolean;
}) {
  if (!crawl) {
    return (
      <box flexGrow={1} alignItems="center" justifyContent="center">
        <text fg={palette.muted}>Loading crawl…</text>
      </box>
    );
  }

  const visible = results?.items.slice(0, narrow ? 8 : 14) ?? [];
  const activity = crawl.lifecycle === "active"
    ? activeFrame(motionTick, reducedMotion)
    : "●";
  return (
    <box flexGrow={1} flexDirection="column" gap={1}>
      <box
        title={` ${crawl.crawl_id} `}
        titleColor={lifecycleColor(crawl.lifecycle)}
        border
        borderStyle="rounded"
        borderColor={palette.border}
        backgroundColor={palette.panel}
        paddingLeft={1}
        paddingRight={1}
        height={7}
        flexDirection="column"
      >
        <text>
          <span fg={lifecycleColor(crawl.lifecycle)}>{activity} </span>
          <span fg={palette.muted}>state </span>
          <span fg={lifecycleColor(crawl.lifecycle)}>{crawl.lifecycle}</span>
          <span fg={palette.muted}>   results </span>
          <span fg={palette.text}>{crawl.result_count}</span>
          <span fg={palette.muted}>   engine </span>
          <span fg={palette.purple}>{engineSummary(results)}</span>
        </text>
        <text>
          <span fg={palette.muted}>done </span>{crawl.stats.done}
          <span fg={palette.muted}>   queued </span>{crawl.stats.queued}
          <span fg={palette.muted}>   leased </span>{crawl.stats.leased}
          <span fg={palette.muted}>   failed </span>
          <span fg={crawl.stats.failed ? palette.bad : palette.text}>{crawl.stats.failed}</span>
        </text>
      </box>

      <box
        title=" Results "
        titleColor={palette.accent}
        border
        borderStyle="rounded"
        borderColor={palette.border}
        backgroundColor={palette.panel}
        paddingLeft={1}
        paddingRight={1}
        flexGrow={1}
        flexDirection="column"
      >
        {visible.length === 0 ? (
          <text fg={palette.muted}>No stored pages yet.</text>
        ) : (
          visible.map((item) => {
            const status = item.status_code == null ? "---" : String(item.status_code);
            const title = item.title || item.url;
            const maxTitle = narrow ? 48 : 84;
            const shown = title.length > maxTitle ? `${title.slice(0, maxTitle - 1)}…` : title;
            return (
              <text key={item.url}>
                <span fg={item.engine === "browser" ? palette.purple : palette.good}>
                  {item.engine === "browser" ? "◈" : "◇"}
                </span>
                <span fg={palette.muted}> {status} </span>
                <span fg={palette.text}>{shown}</span>
              </text>
            );
          })
        )}
      </box>
    </box>
  );
}

function NewCrawl({
  form,
  focus,
  onForm,
  onFocus,
  onSubmit,
  submitting,
}: {
  form: NewCrawlForm;
  focus: number;
  onForm: (next: NewCrawlForm) => void;
  onFocus: (next: number) => void;
  onSubmit: () => void;
  submitting: boolean;
}) {
  return (
    <box flexGrow={1} alignItems="center" justifyContent="center">
      <box
        title=" New crawl "
        titleColor={palette.accent}
        border
        borderStyle="double"
        borderColor={palette.accent}
        backgroundColor={palette.panel}
        width={68}
        height={17}
        flexDirection="column"
        padding={1}
        gap={1}
      >
        <text fg={palette.muted}>Seed URL</text>
        <box border borderColor={focus === 0 ? palette.accent : palette.border} height={3}>
          <input
            placeholder="https://example.com/"
            focused={focus === 0}
            onInput={(url) => onForm({ ...form, url })}
            onSubmit={() => onFocus(1)}
          />
        </box>
        <box flexDirection="row" gap={2}>
          <box flexDirection="column" flexGrow={1}>
            <text fg={palette.muted}>Crawl ID (optional)</text>
            <box border borderColor={focus === 1 ? palette.accent : palette.border} height={3}>
              <input
                placeholder="auto-generated"
                focused={focus === 1}
                onInput={(crawlId) => onForm({ ...form, crawlId })}
                onSubmit={() => onFocus(2)}
              />
            </box>
          </box>
          <box flexDirection="column" width={18}>
            <text fg={palette.muted}>Max pages</text>
            <box border borderColor={focus === 2 ? palette.accent : palette.border} height={3}>
              <input
                placeholder="100"
                focused={focus === 2}
                onInput={(maxPages) => onForm({ ...form, maxPages })}
                onSubmit={onSubmit}
              />
            </box>
          </box>
        </box>
        <text fg={submitting ? palette.warn : palette.muted}>
          {submitting ? "Submitting crawl…" : "Enter starts • Tab changes field • Esc cancels"}
        </text>
      </box>
    </box>
  );
}

function Help() {
  return (
    <box flexGrow={1} alignItems="center" justifyContent="center">
      <box
        title=" Keyboard "
        titleColor={palette.accent}
        border
        borderStyle="rounded"
        borderColor={palette.border}
        backgroundColor={palette.panel}
        width={54}
        height={17}
        padding={1}
        flexDirection="column"
      >
        <text><span fg={palette.accent}>↑ / ↓</span>  select crawl</text>
        <text><span fg={palette.accent}>Enter</span>  inspect selected crawl</text>
        <text><span fg={palette.accent}>n</span>      new crawl</text>
        <text><span fg={palette.accent}>r</span>      refresh</text>
        <text><span fg={palette.accent}>p</span>      pause/resume in detail</text>
        <text><span fg={palette.accent}>x</span>      cancel in detail</text>
        <text><span fg={palette.accent}>Esc</span>    back</text>
        <text><span fg={palette.accent}>?</span>      this help</text>
        <text><span fg={palette.accent}>q</span>      quit dashboard</text>
        <text fg={palette.muted}>Set SYNCRAWLER_TUI_REDUCED_MOTION=true for static motion.</text>
      </box>
    </box>
  );
}

function Footer({ message, error, view }: { message: string; error: string; view: View }) {
  const shortcuts = view === "dashboard"
    ? "↑↓ select  Enter inspect  n new  r refresh  ? help  q quit"
    : view === "detail"
      ? "p pause/resume  x cancel  r refresh  Esc back"
      : "Esc back";
  return (
    <box height={2} paddingLeft={1} flexDirection="column">
      <text fg={error ? palette.bad : message ? palette.good : palette.muted}>
        {error || message || shortcuts}
      </text>
    </box>
  );
}

function App() {
  const api = useMemo(() => new SyndCrawlerApi(), []);
  const reducedMotion = useMemo(() => reducedMotionFromEnv(), []);
  const { width } = useTerminalDimensions();
  const narrow = width < 100;
  const [motionTick, setMotionTick] = useState(0);
  const [view, setView] = useState<View>("dashboard");
  const [health, setHealth] = useState<Health | null>(null);
  const [crawls, setCrawls] = useState<CrawlStatus[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [detail, setDetail] = useState<CrawlStatus | null>(null);
  const [results, setResults] = useState<CrawlResultsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [form, setForm] = useState<NewCrawlForm>({ url: "", crawlId: "", maxPages: "" });
  const [formFocus, setFormFocus] = useState(0);
  const [submitting, setSubmitting] = useState(false);

  const flash = useCallback((next: string, isError = false) => {
    if (isError) {
      setError(next);
      setMessage("");
    } else {
      setMessage(next);
      setError("");
    }
  }, []);

  const refreshDashboard = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [nextHealth, page] = await Promise.all([api.health(), api.listCrawls(50, 0)]);
      setHealth(nextHealth);
      setCrawls(page.items);
      setSelectedIndex((current) => Math.min(current, Math.max(0, page.items.length - 1)));
      if (!quiet) flash(`Loaded ${page.items.length} of ${page.total} crawls`);
    } catch (nextError) {
      setHealth(null);
      flash(safeError(nextError), true);
    } finally {
      setLoading(false);
    }
  }, [api, flash]);

  const refreshDetail = useCallback(async (crawlId: string, quiet = false) => {
    try {
      const [nextStatus, nextResults] = await Promise.all([
        api.status(crawlId),
        api.results(crawlId, 100, 0),
      ]);
      setDetail(nextStatus);
      setResults(nextResults);
      if (!quiet) flash(`Refreshed ${crawlId}`);
    } catch (nextError) {
      flash(safeError(nextError), true);
    }
  }, [api, flash]);

  useEffect(() => {
    if (reducedMotion) return;
    const timer = setInterval(() => {
      setMotionTick((current) => current + 1);
    }, 140);
    return () => clearInterval(timer);
  }, [reducedMotion]);

  useEffect(() => {
    void refreshDashboard();
    const timer = setInterval(() => {
      if (view === "dashboard") void refreshDashboard(true);
      if (view === "detail" && detail) void refreshDetail(detail.crawl_id, true);
    }, 1500);
    return () => clearInterval(timer);
  }, [detail, refreshDashboard, refreshDetail, view]);

  const openSelected = useCallback(() => {
    const selected = crawls[selectedIndex];
    if (!selected) return;
    setDetail(selected);
    setResults(null);
    setView("detail");
    void refreshDetail(selected.crawl_id);
  }, [crawls, refreshDetail, selectedIndex]);

  const submitNew = useCallback(async () => {
    const url = form.url.trim();
    if (!url) {
      flash("Seed URL is required", true);
      setFormFocus(0);
      return;
    }
    const parsedMax = form.maxPages.trim() ? Number.parseInt(form.maxPages.trim(), 10) : 100;
    if (!Number.isFinite(parsedMax) || parsedMax <= 0) {
      flash("Max pages must be a positive integer", true);
      setFormFocus(2);
      return;
    }

    setSubmitting(true);
    try {
      const created = await api.submit({
        urls: [url],
        crawl_id: form.crawlId.trim() || undefined,
        follow_links: true,
        max_pages: parsedMax,
      });
      setForm({ url: "", crawlId: "", maxPages: "" });
      setFormFocus(0);
      setDetail(created);
      setResults(null);
      setView("detail");
      flash(`Started ${created.crawl_id}`);
      await refreshDetail(created.crawl_id, true);
    } catch (nextError) {
      flash(safeError(nextError), true);
    } finally {
      setSubmitting(false);
    }
  }, [api, flash, form, refreshDetail]);

  const togglePause = useCallback(async () => {
    if (!detail) return;
    try {
      const next = detail.lifecycle === "paused"
        ? await api.resume(detail.crawl_id)
        : await api.pause(detail.crawl_id);
      setDetail(next);
      flash(`${next.crawl_id} is ${next.lifecycle}`);
    } catch (nextError) {
      flash(safeError(nextError), true);
    }
  }, [api, detail, flash]);

  const cancel = useCallback(async () => {
    if (!detail) return;
    try {
      const next = await api.cancel(detail.crawl_id);
      setDetail(next);
      flash(`${next.crawl_id} cancelled`);
    } catch (nextError) {
      flash(safeError(nextError), true);
    }
  }, [api, detail, flash]);

  useKeyboard((key) => {
    if (view === "new") {
      if (key.name === "escape") {
        setView("dashboard");
        flash("New crawl cancelled");
      } else if (key.name === "tab") {
        setFormFocus((current) => (current + 1) % 3);
      }
      return;
    }

    if (view === "help") {
      if (key.name === "escape" || key.name === "q" || key.name === "?") {
        setView("dashboard");
      }
      return;
    }

    if (view === "detail") {
      if (key.name === "escape" || key.name === "backspace") {
        setView("dashboard");
        void refreshDashboard(true);
      } else if (key.name === "r" && detail) {
        void refreshDetail(detail.crawl_id);
      } else if (key.name === "p") {
        void togglePause();
      } else if (key.name === "x") {
        void cancel();
      }
      return;
    }

    if (key.name === "q" || key.name === "escape") process.exit(0);
    if (key.name === "n") {
      setView("new");
      setFormFocus(0);
      setError("");
      setMessage("");
    } else if (key.name === "?") {
      setView("help");
    } else if (key.name === "r") {
      void refreshDashboard();
    } else if (key.name === "up" || key.name === "k") {
      setSelectedIndex((current) => Math.max(0, current - 1));
    } else if (key.name === "down" || key.name === "j") {
      setSelectedIndex((current) => Math.min(Math.max(0, crawls.length - 1), current + 1));
    } else if (key.name === "return" || key.name === "enter") {
      openSelected();
    }
  });

  return (
    <box
      width="100%"
      height="100%"
      backgroundColor={palette.bg}
      flexDirection="column"
      padding={1}
      gap={1}
    >
      <Header
        health={health}
        apiUrl={api.baseUrl}
        motionTick={motionTick}
        reducedMotion={reducedMotion}
      />
      {view === "dashboard" && (
        <Dashboard
          crawls={crawls}
          selectedIndex={selectedIndex}
          narrow={narrow}
          loading={loading}
          motionTick={motionTick}
          reducedMotion={reducedMotion}
        />
      )}
      {view === "detail" && (
        <Detail
          crawl={detail}
          results={results}
          narrow={narrow}
          motionTick={motionTick}
          reducedMotion={reducedMotion}
        />
      )}
      {view === "new" && (
        <NewCrawl
          form={form}
          focus={formFocus}
          onForm={setForm}
          onFocus={setFormFocus}
          onSubmit={() => void submitNew()}
          submitting={submitting}
        />
      )}
      {view === "help" && <Help />}
      <Footer message={message} error={error} view={view} />
    </box>
  );
}

const renderer = await createCliRenderer({ exitOnCtrlC: true });
createRoot(renderer).render(<App />);
