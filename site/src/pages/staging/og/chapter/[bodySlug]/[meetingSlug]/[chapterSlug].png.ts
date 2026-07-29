import type { APIRoute, GetStaticPaths } from "astro";
import { getChapter, getRecentChapterRoutes } from "@lib/data";
import { chapterCard } from "@lib/og";

// Static-recent window OG cards. Prerendered for the newest meetings and
// relocated to /og/chapter/... by the build hook; older chapters keep the SSR
// OG route at the same URL.
export const prerender = true;

export const getStaticPaths: GetStaticPaths = () =>
  getRecentChapterRoutes().map((route) => ({
    params: {
      bodySlug: route.bodySlug,
      meetingSlug: route.meetingSlug,
      chapterSlug: route.chapterSlug,
    },
  }));

export const GET: APIRoute = async ({ params }) => {
  const found = getChapter(
    String(params.bodySlug),
    String(params.meetingSlug),
    String(params.chapterSlug),
  );
  if (!found) {
    return new Response("Not found", { status: 404 });
  }
  const png = await chapterCard(found.meeting, found.chapter);
  return new Response(new Uint8Array(png), {
    headers: { "Content-Type": "image/png" },
  });
};
