import { initWasm, Resvg } from "@resvg/resvg-wasm";
import satori, { init as initSatori } from "satori/standalone";
import resvgWasm from "@resvg/resvg-wasm/index_bg.wasm";
import yogaWasm from "satori/yoga.wasm";
import inter500 from "@fontsource/inter/files/inter-latin-500-normal.woff?bytes";
import inter600 from "@fontsource/inter/files/inter-latin-600-normal.woff?bytes";
import sourceSerif600 from "@fontsource/source-serif-4/files/source-serif-4-latin-600-normal.woff?bytes";
import sourceSerif700 from "@fontsource/source-serif-4/files/source-serif-4-latin-700-normal.woff?bytes";
import { chapterTypeLabel, formatDate, formatDuration } from "./format";
import type { Chapter, Meeting } from "./types";

// Card dimensions match the Open Graph 1.91:1 recommendation.
const WIDTH = 1200;
const HEIGHT = 630;

// Palette lifted from tailwind.config.mjs so cards match the site chrome.
const COLORS = {
  paper: "#fbf8f1",
  ink: "#141311",
  muted: "#625f58",
  line: "#ded7ca",
  civic: "#174a7c",
  wash: "#f2ede3",
};

// satori needs woff/ttf (not woff2); fontsource ships latin .woff files.
const fonts = [
  {
    name: "Source Serif 4",
    data: bytesToArrayBuffer(sourceSerif600),
    weight: 600 as const,
    style: "normal" as const,
  },
  {
    name: "Source Serif 4",
    data: bytesToArrayBuffer(sourceSerif700),
    weight: 700 as const,
    style: "normal" as const,
  },
  {
    name: "Inter",
    data: bytesToArrayBuffer(inter500),
    weight: 500 as const,
    style: "normal" as const,
  },
  {
    name: "Inter",
    data: bytesToArrayBuffer(inter600),
    weight: 600 as const,
    style: "normal" as const,
  },
];

let resvgReady: Promise<void> | undefined;
let satoriReady: Promise<void> | undefined;

function bytesToArrayBuffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
}

async function ensureResvg(): Promise<void> {
  resvgReady ??= initWasm(resvgWasm);
  await resvgReady;
}

async function ensureSatori(): Promise<void> {
  satoriReady ??= initSatori(yogaWasm);
  await satoriReady;
}

// Minimal hyperscript so we can build the satori element tree from a .ts file
// without a JSX runtime.
type Node = {
  type: string;
  props: { style?: Record<string, unknown>; children?: unknown } & Record<string, unknown>;
};

function h(
  type: string,
  style: Record<string, unknown>,
  children?: unknown,
): Node {
  return { type, props: { style, ...(children === undefined ? {} : { children }) } };
}

function clamp(text: string, max: number): string {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

interface CardOptions {
  eyebrow?: string;
  badge?: string;
  headline: string;
  meta?: string;
  footnote?: string;
  headlineSize?: number;
}

function card(options: CardOptions): Node {
  const { eyebrow, badge, headline, meta, footnote, headlineSize = 68 } = options;

  const topRow: Node[] = [];
  if (badge) {
    topRow.push(
      h(
        "div",
        {
          display: "flex",
          alignSelf: "flex-start",
          border: `2px solid ${COLORS.line}`,
          borderRadius: 999,
          backgroundColor: COLORS.wash,
          padding: "8px 20px",
          fontFamily: "Inter",
          fontWeight: 600,
          fontSize: 22,
          letterSpacing: 2,
          textTransform: "uppercase",
          color: COLORS.civic,
        },
        badge,
      ),
    );
  }
  if (eyebrow) {
    topRow.push(
      h(
        "div",
        {
          display: "flex",
          fontFamily: "Inter",
          fontWeight: 600,
          fontSize: 24,
          letterSpacing: 4,
          textTransform: "uppercase",
          color: COLORS.civic,
        },
        eyebrow,
      ),
    );
  }

  const bodyChildren: Node[] = [];
  if (topRow.length) {
    bodyChildren.push(
      h("div", { display: "flex", flexDirection: "column", gap: 20 }, topRow),
    );
  }
  bodyChildren.push(
    h(
      "div",
      {
        display: "flex",
        fontFamily: "Source Serif 4",
        fontWeight: 700,
        fontSize: headlineSize,
        lineHeight: 1.08,
        color: COLORS.ink,
      },
      headline,
    ),
  );
  if (meta) {
    bodyChildren.push(
      h(
        "div",
        {
          display: "flex",
          fontFamily: "Inter",
          fontWeight: 500,
          fontSize: 30,
          color: COLORS.muted,
        },
        meta,
      ),
    );
  }

  const footer = h(
    "div",
    {
      display: "flex",
      justifyContent: "space-between",
      alignItems: "flex-end",
    },
    [
      h(
        "div",
        {
          display: "flex",
          fontFamily: "Source Serif 4",
          fontWeight: 700,
          fontSize: 30,
          color: COLORS.ink,
        },
        "Council Discourse",
      ),
      h(
        "div",
        {
          display: "flex",
          fontFamily: "Inter",
          fontWeight: 500,
          fontSize: 24,
          color: COLORS.muted,
        },
        footnote ?? "NYC Council meetings, chaptered by time",
      ),
    ],
  );

  return h(
    "div",
    {
      width: WIDTH,
      height: HEIGHT,
      display: "flex",
      backgroundColor: COLORS.paper,
    },
    [
      // Navy accent bar down the left edge.
      h("div", { display: "flex", width: 16, height: HEIGHT, backgroundColor: COLORS.civic }),
      h(
        "div",
        {
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          flex: 1,
          padding: "72px 80px",
        },
        [
          h(
            "div",
            { display: "flex", flexDirection: "column", gap: 28 },
            bodyChildren,
          ),
          footer,
        ],
      ),
    ],
  );
}

async function render(node: Node): Promise<Uint8Array> {
  await ensureSatori();
  await ensureResvg();
  const svg = await satori(node as unknown as Parameters<typeof satori>[0], {
    width: WIDTH,
    height: HEIGHT,
    fonts,
  });
  const resvg = new Resvg(svg, { fitTo: { mode: "width", value: WIDTH } });
  const rendered = resvg.render();
  const png = rendered.asPng();
  rendered.free();
  resvg.free();
  return png;
}

// Absolute-path builders for the emitted PNG endpoints. Kept next to the
// renderers so the URL scheme and the getStaticPaths routes stay in sync.
export const homeOgPath = "/og/home.png";

export function meetingOgPath(bodySlug: string, meetingSlug: string): string {
  return `/og/meeting/${bodySlug}/${meetingSlug}.png`;
}

export function chapterOgPath(
  bodySlug: string,
  meetingSlug: string,
  chapterSlug: string,
): string {
  return `/og/chapter/${bodySlug}/${meetingSlug}/${chapterSlug}.png`;
}

export function homeCard(): Promise<Uint8Array> {
  return render(
    card({
      eyebrow: "Public meeting record",
      headline: "NYC Council meetings,\nchaptered by time.",
      meta: "Summaries, chapters, speaker-attributed transcripts, and click-to-seek video.",
      headlineSize: 76,
    }),
  );
}

export function meetingCard(meeting: Meeting): Promise<Uint8Array> {
  const meta = `${formatDate(meeting.date)}${meeting.time ? ` · ${meeting.time}` : ""} · ${formatDuration(
    meeting.duration_sec,
  )}`;
  return render(
    card({
      eyebrow: meeting.body,
      headline: clamp(meeting.title, 90),
      meta,
    }),
  );
}

export function chapterCard(meeting: Meeting, chapter: Chapter): Promise<Uint8Array> {
  const meta = `${meeting.title} · ${formatDate(meeting.date)}`;
  return render(
    card({
      badge: chapterTypeLabel(chapter.type),
      headline: clamp(chapter.title, 120),
      meta: clamp(meta, 90),
      headlineSize: 60,
    }),
  );
}
