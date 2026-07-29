import type { APIRoute, GetStaticPaths } from "astro";
import { getBodies } from "@lib/data";
import { meetingCard } from "@lib/og";

export const getStaticPaths: GetStaticPaths = () =>
  getBodies().flatMap((body) =>
    body.meetings.map((meeting) => ({
      params: { bodySlug: body.slug, meetingSlug: meeting.slug },
      props: { meeting },
    })),
  );

export const GET: APIRoute = async ({ props }) => {
  const png = await meetingCard(props.meeting);
  return new Response(new Uint8Array(png), {
    headers: { "Content-Type": "image/png" },
  });
};
