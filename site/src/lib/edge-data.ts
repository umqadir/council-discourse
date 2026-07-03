import type { Chapter, Meeting } from "./types";
import { chapterRoutes } from "virtual:chapter-manifest";

export interface ChapterRouteRecord {
  bodySlug: string;
  meetingSlug: string;
  chapterSlug: string;
  dataKey: string;
  dataVersion: string;
  pathname: string;
  ogPathname: string;
}

export interface R2ObjectBody {
  text(): Promise<string>;
}

export interface R2BucketBinding {
  get(key: string): Promise<R2ObjectBody | null>;
}

export interface EdgeEnv {
  DATA?: R2BucketBinding;
}

export interface CloudflareRuntime {
  env?: EdgeEnv;
  caches?: CacheStorage;
  ctx?: {
    waitUntil(promise: Promise<unknown>): void;
  };
}

export type EdgeChapterResult =
  | {
      status: "ok";
      record: ChapterRouteRecord;
      meeting: Meeting;
      chapter: Chapter;
      index: number;
    }
  | { status: "not-found" }
  | { status: "missing-binding"; binding: "DATA" };

const routeRecords = chapterRoutes as ChapterRouteRecord[];

const byParams = new Map(
  routeRecords.map((route) => [
    paramsKey(route.bodySlug, route.meetingSlug, route.chapterSlug),
    route,
  ]),
);

const byPathname = new Map<string, ChapterRouteRecord>();
for (const route of routeRecords) {
  byPathname.set(route.pathname, route);
  byPathname.set(route.pathname.replace(/\/$/, ""), route);
  byPathname.set(route.ogPathname, route);
}

export function getRuntime(locals: unknown): CloudflareRuntime | undefined {
  const candidate = locals as { runtime?: CloudflareRuntime };
  return candidate.runtime;
}

export function getChapterRouteRecord(
  bodySlug: string | undefined,
  meetingSlug: string | undefined,
  chapterSlug: string | undefined,
): ChapterRouteRecord | undefined {
  if (!bodySlug || !meetingSlug || !chapterSlug) {
    return undefined;
  }
  return byParams.get(paramsKey(bodySlug, meetingSlug, chapterSlug));
}

export function getChapterRouteRecordForPath(pathname: string): ChapterRouteRecord | undefined {
  return byPathname.get(pathname);
}

export async function loadEdgeChapter(
  params: {
    bodySlug?: string;
    meetingSlug?: string;
    chapterSlug?: string;
  },
  env: EdgeEnv | undefined,
): Promise<EdgeChapterResult> {
  const record = getChapterRouteRecord(params.bodySlug, params.meetingSlug, params.chapterSlug);
  if (!record) {
    return { status: "not-found" };
  }

  const bucket = env?.DATA;
  if (!bucket) {
    return { status: "missing-binding", binding: "DATA" };
  }

  const object = await bucket.get(record.dataKey);
  if (!object) {
    return { status: "not-found" };
  }

  const meeting = JSON.parse(await object.text()) as Meeting;
  const index = meeting.chapters.findIndex((chapter) => chapter.slug === params.chapterSlug);
  if (index === -1) {
    return { status: "not-found" };
  }

  return {
    status: "ok",
    record,
    meeting,
    chapter: meeting.chapters[index],
    index,
  };
}

function paramsKey(bodySlug: string, meetingSlug: string, chapterSlug: string): string {
  return `${bodySlug}/${meetingSlug}/${chapterSlug}`;
}
