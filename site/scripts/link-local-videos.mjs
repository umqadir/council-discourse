import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const siteDir = path.resolve(scriptDir, "..");
const rootDir = path.resolve(siteDir, "..");
const meetingsDir = path.join(rootDir, "data", "meetings");
const videosDir = path.join(siteDir, "public", "videos");

fs.mkdirSync(videosDir, { recursive: true });

let linked = 0;
if (fs.existsSync(meetingsDir)) {
  for (const meetingKey of fs.readdirSync(meetingsDir)) {
    const source = path.join(meetingsDir, meetingKey, "video-web.mp4");
    if (!fs.existsSync(source)) {
      continue;
    }
    const target = path.join(videosDir, `${meetingKey}.mp4`);
    try {
      fs.unlinkSync(target);
    } catch (error) {
      if (error.code !== "ENOENT") {
        throw error;
      }
    }
    fs.symlinkSync(path.relative(videosDir, source), target);
    linked += 1;
  }
}

console.log(`linked ${linked} local video files`);
