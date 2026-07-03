import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import cloudflare from "@astrojs/cloudflare";
import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";

const site = "https://council-discourse.pages.dev";
const projectDir = fileURLToPath(new URL(".", import.meta.url));
const meetingDataDir = path.join(projectDir, "src", "data", "meetings");
const r2DataPrefix = "data/meetings";
const redirectedBodyList = new URL("/meetings/new-york-city-council/", site).href;
const chapterManifest = readChapterManifest();
const chapterSitemapPages = chapterManifest.routes.map((route) =>
  new URL(route.pathname, site).href,
);
// Match getRecentChapterRoutes() in src/lib/data.ts: newest meetings within a
// day window, bounded by a chapter budget so dist stays well under the asset
// ceiling.
const RECENT_WINDOW_DAYS = 90;
const RECENT_CHAPTER_BUDGET = 6000;
const DAY_MS = 24 * 60 * 60 * 1000;

// Newest meetings whose chapter pages + OG images are prerendered statically
// (mirrors getRecentChapterRoutes() in src/lib/data.ts).
const recentMeetings = readRecentMeetings();

export default defineConfig({
  output: "static",
  adapter: cloudflare({
    imageService: "passthrough",
  }),
  site,
  integrations: [
    sitemap({
      customPages: chapterSitemapPages,
      filter(page) {
        const normalized = page.endsWith("/") ? page : `${page}/`;
        if (normalized === redirectedBodyList) {
          return false;
        }
        // Internal staging routes for the static-recent window; the canonical
        // chapter URLs are supplied via customPages instead.
        return !normalized.includes("/staging/");
      },
    }),
    staticRecentWindowIntegration(recentMeetings),
  ],
  vite: {
    plugins: [chapterManifestPlugin(chapterManifest), binaryBytesPlugin()],
  },
});

function readRecentMeetings() {
  if (!fs.existsSync(meetingDataDir)) {
    return [];
  }

  const meetings = fs
    .readdirSync(meetingDataDir)
    .filter((name) => name.endsWith(".json"))
    .map((name) => JSON.parse(fs.readFileSync(path.join(meetingDataDir, name), "utf8")))
    .sort((a, b) => `${b.date} ${b.time}`.localeCompare(`${a.date} ${a.time}`));

  const newest = meetings[0];
  if (!newest) {
    return [];
  }

  const cutoff = new Date(`${newest.date}T12:00:00`).getTime() - RECENT_WINDOW_DAYS * DAY_MS;
  const recent = [];
  let budget = RECENT_CHAPTER_BUDGET;

  for (const meeting of meetings) {
    const meetingTime = new Date(`${meeting.date}T12:00:00`).getTime();
    const chapterCount = (meeting.chapters ?? []).length;
    if (meetingTime < cutoff || chapterCount > budget) {
      break;
    }
    budget -= chapterCount;
    recent.push({ bodySlug: slugify(meeting.body), meetingSlug: meeting.slug });
  }

  return recent;
}

// Relocates the /staging/* output for recent chapters to the canonical
// /meetings/.../chapter/... and /og/chapter/... URLs, then marks those path
// prefixes as static assets in _routes.json so Cloudflare Pages serves them
// directly instead of invoking the SSR worker. Older chapters remain worker
// routes at identical URLs. Exclusions are per-meeting wildcards to stay well
// under the 100-rule _routes.json limit.
function staticRecentWindowIntegration(recent) {
  return {
    name: "static-recent-window",
    hooks: {
      "astro:build:done": async ({ dir, logger }) => {
        const distDir = fileURLToPath(dir);
        const prerenderDir = path.join(distDir, "staging");
        if (!fs.existsSync(prerenderDir)) {
          logger.warn("no staging output found; static-recent-window skipped");
          return;
        }

        let movedPages = 0;
        let movedImages = 0;
        for (const { bodySlug, meetingSlug } of recent) {
          movedPages += relocateDir(
            path.join(prerenderDir, "chapter", bodySlug, meetingSlug),
            path.join(distDir, "meetings", bodySlug, meetingSlug, "chapter"),
          );
          movedImages += relocateDir(
            path.join(prerenderDir, "og", "chapter", bodySlug, meetingSlug),
            path.join(distDir, "og", "chapter", bodySlug, meetingSlug),
          );
        }

        fs.rmSync(prerenderDir, { recursive: true, force: true });

        const routesPath = path.join(distDir, "_routes.json");
        if (fs.existsSync(routesPath)) {
          const routes = JSON.parse(fs.readFileSync(routesPath, "utf8"));
          const exclude = new Set(routes.exclude ?? []);
          // Keep Astro's "/staging/*" exclude: the relocated output no longer
          // exists there, so those internal URLs resolve to a static-asset 404
          // instead of ever reaching the SSR worker.
          for (const { bodySlug, meetingSlug } of recent) {
            exclude.add(`/meetings/${bodySlug}/${meetingSlug}/chapter/*`);
            exclude.add(`/og/chapter/${bodySlug}/${meetingSlug}/*`);
          }
          routes.exclude = [...exclude];
          fs.writeFileSync(routesPath, `${JSON.stringify(routes, null, 2)}\n`);
        }

        logger.info(
          `static-recent-window: ${recent.length} meetings, ${movedPages} chapter pages, ${movedImages} OG images prerendered`,
        );
      },
    },
  };
}

// Moves every file/dir under `from` into `to`, then removes the empty source.
function relocateDir(from, to) {
  if (!fs.existsSync(from)) {
    return 0;
  }
  fs.mkdirSync(to, { recursive: true });
  let count = 0;
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    const src = path.join(from, entry.name);
    const dest = path.join(to, entry.name);
    fs.rmSync(dest, { recursive: true, force: true });
    fs.renameSync(src, dest);
    count += entry.isDirectory() ? 1 : entry.name.endsWith(".png") || entry.name.endsWith(".html") ? 1 : 0;
  }
  fs.rmSync(from, { recursive: true, force: true });
  return count;
}

function readChapterManifest() {
  if (!fs.existsSync(meetingDataDir)) {
    return { routes: [] };
  }

  const routes = [];
  for (const name of fs.readdirSync(meetingDataDir).sort()) {
    if (!name.endsWith(".json")) {
      continue;
    }
    const filePath = path.join(meetingDataDir, name);
    const text = fs.readFileSync(filePath, "utf8");
    const meeting = JSON.parse(text);
    const bodySlug = slugify(meeting.body);
    const meetingSlug = meeting.slug;
    const version = crypto.createHash("sha256").update(text).digest("hex").slice(0, 16);
    const dataKey = `${r2DataPrefix}/${meetingSlug}.${version}.json`;

    for (const chapter of meeting.chapters ?? []) {
      const chapterSlug = chapter.slug;
      routes.push({
        bodySlug,
        meetingSlug,
        chapterSlug,
        dataKey,
        dataVersion: version,
        pathname: `/meetings/${bodySlug}/${meetingSlug}/chapter/${chapterSlug}/`,
        ogPathname: `/og/chapter/${bodySlug}/${meetingSlug}/${chapterSlug}.png`,
      });
    }
  }
  return { routes };
}

function slugify(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function chapterManifestPlugin(manifest) {
  const publicId = "virtual:chapter-manifest";
  const resolvedId = "\0chapter-manifest";
  return {
    name: "chapter-manifest",
    resolveId(id) {
      return id === publicId ? resolvedId : null;
    },
    load(id) {
      if (id !== resolvedId) {
        return null;
      }
      return `export const chapterRoutes = ${JSON.stringify(manifest.routes)};`;
    },
  };
}

function binaryBytesPlugin() {
  const query = "?bytes";
  return {
    name: "binary-bytes",
    enforce: "pre",
    async resolveId(source, importer) {
      if (!source.endsWith(query)) {
        return null;
      }
      const bareSource = source.slice(0, -query.length);
      const resolved = await this.resolve(bareSource, importer, { skipSelf: true });
      return resolved ? `${resolved.id}${query}` : null;
    },
    load(id) {
      if (!id.endsWith(query)) {
        return null;
      }
      const filePath = id.slice(0, -query.length);
      const encoded = fs.readFileSync(filePath).toString("base64");
      return [
        `const encoded = ${JSON.stringify(encoded)};`,
        "const bytes = Uint8Array.from(atob(encoded), (char) => char.charCodeAt(0));",
        "export default bytes;",
      ].join("\n");
    },
  };
}
