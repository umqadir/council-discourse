/// <reference types="astro/client" />

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
