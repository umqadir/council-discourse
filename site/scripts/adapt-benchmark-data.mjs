import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SITE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const REPO_ROOT = path.resolve(SITE_ROOT, "..");
const BENCHMARK_DIR = path.join(REPO_ROOT, "data", "benchmark");
const OUT_DIR = path.join(SITE_ROOT, "src", "data", "meetings");
const CHAPTER_MODEL = "gemini-3.5-flash";

const MEETING_OVERRIDES = {
  "2025-04-23-transportation": {
    body: "New York City Council",
    title: "Committee on Transportation and Infrastructure",
    slug: "2025-04-23-1000-am-committee-on-transportation-and-infrastructure",
    tags: ["HEARING"],
    summary: [
      "A joint hearing examined Dining Out NYC, the permanent outdoor dining program that replaced the temporary pandemic-era program.",
      "Discussion focused on application complexity, approval delays, restaurant costs, seasonal roadway dining rules, clearance requirements, and accessibility.",
      "The meeting includes opening remarks, DOT testimony, council member questioning, and public testimony from restaurant, disability, transportation, and neighborhood advocates.",
    ],
  },
  "2025-04-24-stated": {
    body: "New York City Council",
    title: "Stated Meeting",
    slug: "2025-04-24-0130-pm-stated-meeting",
    tags: ["STATED_MEETING", "VOTE", "LAND_USE"],
    summary: [
      "The Council held a stated meeting to vote on legislation, introduce new bills, and handle land use and procedural business.",
      "Members approved measures including bills supporting transgender, gender non-conforming, and non-binary New Yorkers and measures regulating non-essential helicopter flights.",
      "The Council also considered land use items, member remarks, and introductions covering deed theft, stormwater management, public safety, and housing administration.",
    ],
  },
};

function existingGeneratedData() {
  if (!fs.existsSync(OUT_DIR)) {
    return false;
  }
  return fs.readdirSync(OUT_DIR).some((name) => name.endsWith(".json"));
}

function slugify(value) {
  return String(value)
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/&/g, " and ")
    .replace(/['"]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 96);
}

function parseClock(value) {
  const parts = String(value).split(":").map((part) => Number(part));
  if (parts.length === 2) {
    return parts[0] * 60 + parts[1];
  }
  if (parts.length === 3) {
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  throw new Error(`Unsupported clock value: ${value}`);
}

function roundedSeconds(value) {
  return Number(Number(value).toFixed(3));
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function readCaptions(slugDir) {
  const filePath = path.join(slugDir, "captions-clean.jsonl");
  return fs
    .readFileSync(filePath, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => {
      const row = JSON.parse(line);
      return { t_sec: roundedSeconds(row.t), text: String(row.text || "").trim() };
    });
}

function cleanCaptionText(value) {
  return String(value)
    .replace(/^>>\s*/, "")
    .replace(/\s+/g, " ")
    .trim();
}

function chapterSlug(title, index, seen) {
  const base = slugify(title) || `chapter-${index + 1}`;
  const count = seen.get(base) || 0;
  seen.set(base, count + 1);
  return count === 0 ? base : `${base}-${count + 1}`;
}

function utterancesForChapter(captions, startSec, endSec) {
  const utterances = [];
  for (const caption of captions) {
    if (caption.t_sec < startSec || caption.t_sec >= endSec) {
      continue;
    }
    const startsTurn = caption.text.startsWith(">>");
    const text = cleanCaptionText(caption.text);
    if (!text) {
      continue;
    }

    const last = utterances[utterances.length - 1];
    const needsNew =
      !last ||
      startsTurn ||
      caption.t_sec - last.t_sec > 18 ||
      last.text.length > 280;

    if (needsNew) {
      utterances.push({
        t_sec: caption.t_sec,
        speaker: "Speaker",
        text,
      });
    } else {
      last.text = `${last.text} ${text}`;
    }
  }

  return utterances;
}

function convertMeeting(slugDir) {
  const sourceSlug = path.basename(slugDir);
  const override = MEETING_OVERRIDES[sourceSlug];
  if (!override) {
    throw new Error(`No meeting override configured for ${sourceSlug}`);
  }

  const meeting = readJson(path.join(slugDir, "meeting.json"));
  const chapterResult = readJson(path.join(slugDir, `chapters-${CHAPTER_MODEL}.json`));
  const captions = readCaptions(slugDir);
  const chapterStarts = chapterResult.chapters.map((chapter) => parseClock(chapter.start));
  const seenSlugs = new Map();

  const chapters = chapterResult.chapters.map((chapter, index) => {
    const startSec = chapterStarts[index];
    const nextStartSec = chapterStarts[index + 1] ?? meeting.duration_sec;
    const endSec = Math.max(startSec + 1, nextStartSec);
    return {
      id: String(index + 1).padStart(3, "0"),
      slug: chapterSlug(chapter.title, index, seenSlugs),
      type: chapter.type,
      title: chapter.title,
      summary: chapter.summary,
      start_sec: roundedSeconds(startSec),
      end_sec: roundedSeconds(endSec),
      utterances: utterancesForChapter(captions, startSec, endSec),
    };
  });

  return {
    slug: override.slug,
    body: override.body,
    title: override.title,
    date: meeting.date,
    time: meeting.time,
    duration_sec: roundedSeconds(meeting.duration_sec),
    video: {
      url: `https://vbfast-vod.viebit.com/counciln/${meeting.viebit_hash}/${meeting.viebit_file}.mp4`,
      poster: "/og-placeholder.svg",
    },
    summary: override.summary,
    tags: override.tags,
    chapters,
  };
}

function main() {
  if (!fs.existsSync(BENCHMARK_DIR)) {
    if (existingGeneratedData()) {
      console.warn("data/benchmark not found; using existing generated meeting JSON.");
      return;
    }
    throw new Error("data/benchmark not found and no generated meeting JSON exists.");
  }

  const slugDirs = fs
    .readdirSync(BENCHMARK_DIR)
    .map((name) => path.join(BENCHMARK_DIR, name))
    .filter((entry) => fs.statSync(entry).isDirectory())
    .filter((entry) => fs.existsSync(path.join(entry, "meeting.json")));

  if (!slugDirs.length) {
    if (existingGeneratedData()) {
      console.warn("No benchmark meetings found; using existing generated meeting JSON.");
      return;
    }
    throw new Error("No benchmark meetings found.");
  }

  fs.rmSync(OUT_DIR, { recursive: true, force: true });
  fs.mkdirSync(OUT_DIR, { recursive: true });

  for (const slugDir of slugDirs) {
    const meeting = convertMeeting(slugDir);
    fs.writeFileSync(
      path.join(OUT_DIR, `${meeting.slug}.json`),
      `${JSON.stringify(meeting, null, 2)}\n`,
    );
    console.log(`wrote ${meeting.slug}.json (${meeting.chapters.length} chapters)`);
  }
}

main();
