/// <reference types="astro/client" />

declare global {
  interface Window {
    Alpine?: import("alpinejs").Alpine;
    chapterVideoPlayer?: unknown;
    seekChapterVideo?: (seconds: number | string | undefined) => void;
  }
}

export {};
