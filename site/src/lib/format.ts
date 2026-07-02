import type { MeetingTag } from "./types";

const DATE_FORMAT = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
});

export function formatDate(date: string): string {
  return DATE_FORMAT.format(new Date(`${date}T12:00:00`));
}

export function formatClock(seconds: number): string {
  const safeSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const secs = safeSeconds % 60;
  return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}

export function formatDuration(seconds: number): string {
  const safeSeconds = Math.max(0, Math.round(seconds));
  const hours = Math.floor(safeSeconds / 3600);
  const minutes = Math.floor((safeSeconds % 3600) / 60);
  const secs = safeSeconds % 60;

  if (hours > 0) {
    return minutes > 0 ? `${hours} hr ${minutes} min` : `${hours} hr`;
  }
  if (minutes > 0) {
    return secs > 0 ? `${minutes} min ${secs} sec` : `${minutes} min`;
  }
  return `${secs} sec`;
}

export function meetingTagLabel(tag: MeetingTag): string {
  const labels: Record<MeetingTag, string> = {
    HEARING: "Hearing",
    VOTE: "Vote",
    STATED_MEETING: "Stated Meeting",
    LAND_USE: "Land Use",
  };
  return labels[tag] ?? tag;
}

export function chapterTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    AGENCY_TESTIMONY: "Agency Testimony",
    PROCEDURE: "Procedure",
    QA: "Q&A",
    REMARKS: "Remarks",
    TESTIMONY: "Public Testimony",
    VOTE: "Vote",
  };
  return labels[type] ?? type.replace(/_/g, " ");
}

export function firstSentences(text: string, count = 2, maxChars = 300): string {
  const clean = text.replace(/\s+/g, " ").trim();
  if (!clean) {
    return "";
  }

  const sentences =
    clean.match(/[^.!?]+(?:[.!?]+|$)/g)?.map((sentence) => sentence.trim()).filter(Boolean) ?? [clean];
  const selected: string[] = [];
  const hardMaxChars = Math.round(maxChars * 1.25);

  for (const sentence of sentences.slice(0, count)) {
    const candidate = [...selected, sentence].join(" ");
    if (candidate.length > maxChars && selected.length > 0) {
      break;
    }
    selected.push(sentence);
  }

  const teaser = selected.join(" ").trim() || clean;
  return teaser.length > hardMaxChars ? truncateWords(teaser, maxChars) : teaser;
}

function truncateWords(text: string, maxChars: number): string {
  const clipped = text.slice(0, maxChars).trimEnd();
  const lastSpace = clipped.lastIndexOf(" ");
  const boundary = lastSpace > Math.floor(maxChars * 0.65) ? lastSpace : clipped.length;
  const trimmed = clipped.slice(0, boundary).replace(/[.,;:!?-]+$/, "").trimEnd();
  return `${trimmed}...`;
}
