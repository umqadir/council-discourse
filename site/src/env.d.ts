/// <reference types="astro/client" />

declare module "virtual:chapter-manifest" {
  export const chapterRoutes: import("@lib/edge-data").ChapterRouteRecord[];
}

declare module "*?bytes" {
  const bytes: Uint8Array;
  export default bytes;
}

declare namespace App {
  interface Locals {
    runtime?: import("@lib/edge-data").CloudflareRuntime;
  }
}

declare global {
  interface Window {
    Alpine?: import("alpinejs").Alpine;
    videojs?: (
      element: Element,
      options?: Record<string, unknown>,
    ) => {
      ready: (callback: () => void) => void;
      currentTime: (seconds?: number) => number;
      pause: () => void;
      paused: () => boolean;
    };
    chapterVideoPlayer?: ReturnType<NonNullable<Window["videojs"]>>;
    seekChapterVideo?: (seconds: number | string | undefined) => void;
  }
}

export {};
