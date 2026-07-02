import fs from "node:fs";
import path from "node:path";
import type { BodySummary, Chapter, Meeting } from "./types";
import { slugify } from "./slug";

const DATA_DIR = path.join(process.cwd(), "src", "data", "meetings");

let meetingCache: Meeting[] | undefined;

function compareMeetingsDesc(a: Meeting, b: Meeting): number {
  return `${b.date} ${b.time}`.localeCompare(`${a.date} ${a.time}`);
}

export function bodySlug(meeting: Meeting): string {
  return slugify(meeting.body);
}

export function getMeetings(): Meeting[] {
  if (meetingCache) {
    return meetingCache;
  }
  if (!fs.existsSync(DATA_DIR)) {
    meetingCache = [];
    return meetingCache;
  }

  meetingCache = fs
    .readdirSync(DATA_DIR)
    .filter((name) => name.endsWith(".json"))
    .map((name) => {
      const filePath = path.join(DATA_DIR, name);
      return JSON.parse(fs.readFileSync(filePath, "utf8")) as Meeting;
    })
    .sort(compareMeetingsDesc);

  return meetingCache;
}

export function getBodies(): BodySummary[] {
  const bySlug = new Map<string, BodySummary>();

  for (const meeting of getMeetings()) {
    const slug = bodySlug(meeting);
    const current = bySlug.get(slug);
    if (current) {
      current.meetings.push(meeting);
    } else {
      bySlug.set(slug, { slug, name: meeting.body, meetings: [meeting] });
    }
  }

  return [...bySlug.values()].map((body) => ({
    ...body,
    meetings: body.meetings.sort(compareMeetingsDesc),
  }));
}

export function getMeetingsForBody(slug: string): Meeting[] {
  return getMeetings().filter((meeting) => bodySlug(meeting) === slug);
}

export function getMeeting(slug: string, meetingSlug: string): Meeting | undefined {
  return getMeetings().find(
    (meeting) => bodySlug(meeting) === slug && meeting.slug === meetingSlug,
  );
}

export function getChapter(
  slug: string,
  meetingSlug: string,
  chapterSlug: string,
): { meeting: Meeting; chapter: Chapter; index: number } | undefined {
  const meeting = getMeeting(slug, meetingSlug);
  if (!meeting) {
    return undefined;
  }
  const index = meeting.chapters.findIndex((chapter) => chapter.slug === chapterSlug);
  if (index === -1) {
    return undefined;
  }
  return { meeting, chapter: meeting.chapters[index], index };
}
