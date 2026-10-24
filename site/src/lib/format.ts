import type { MeetingTag } from "./types";

const DATE_FORMAT = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
});

export function formatDate(date: string): string {
  return DATE_FORMAT.format(new Date(`${date}T12:00:00`));
}

// Meeting times are scheduled NYC times, stored as 12-hour strings ("1:00 PM").
export function formatTime(time: string | undefined): string {
  return time ? `${time} ET` : "";
}

// Minutes after midnight for a 12-hour time string; unparseable times sort first.
export function timeToMinutes(time: string | undefined): number {
  const match = /^\s*(\d{1,2}):(\d{2})\s*([AP])\.?M\.?\s*$/i.exec(time ?? "");
  if (!match) {
    return -1;
  }
  const hours = (Number(match[1]) % 12) + (match[3].toUpperCase() === "P" ? 12 : 0);
  return hours * 60 + Number(match[2]);
}

// Diarization leaves unattributed turns as UNKNOWN (or UNKNOWN_part2, ...) and
// caption fallbacks use the bare label "Speaker".
export function displaySpeaker(speaker: string): string {
  const label = speaker.trim();
  return /^unknown(_.*)?$/i.test(label) || /^speaker$/i.test(label) || label === ""
    ? "Unidentified speaker"
    : label;
}

// The pipeline writes "Summary pending." until a summary exists.
export function hasSummaryText(summary: string | undefined): boolean {
  const text = (summary ?? "").trim();
  return text !== "" && text.toLowerCase() !== "summary pending.";
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

