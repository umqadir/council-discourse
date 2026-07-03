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
        return normalized !== redirectedBodyList;
      },
    }),
  ],
  vite: {
    plugins: [chapterManifestPlugin(chapterManifest), binaryBytesPlugin()],
  },
});

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
