import type { APIRoute, GetStaticPaths } from "astro";
import { getBodies } from "@lib/data";
import { chapterCard } from "@lib/og";

export const getStaticPaths: GetStaticPaths = () =>
  getBodies().flatMap((body) =>
    body.meetings.flatMap((meeting) =>
      meeting.chapters.map((chapter) => ({
        params: {
          bodySlug: body.slug,
          meetingSlug: meeting.slug,
          chapterSlug: chapter.slug,
        },
        props: { meeting, chapter },
      })),
    ),
  );

export const GET: APIRoute = async ({ props }) => {
  const png = await chapterCard(props.meeting, props.chapter);
  return new Response(new Uint8Array(png), {
    headers: { "Content-Type": "image/png" },
  });
};
