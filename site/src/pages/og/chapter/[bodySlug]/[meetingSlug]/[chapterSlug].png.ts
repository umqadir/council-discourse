import type { APIRoute } from "astro";
import { getRuntime, loadEdgeChapter } from "@lib/edge-data";
import { chapterCard } from "@lib/og";

export const prerender = false;

export const GET: APIRoute = async ({ params, locals }) => {
  const result = await loadEdgeChapter(params, getRuntime(locals)?.env);
  if (result.status === "not-found") {
    return new Response("Not found", { status: 404 });
  }
  if (result.status === "missing-binding") {
    return new Response(`${result.binding} R2 binding is not configured`, { status: 500 });
  }

  const png = await chapterCard(result.meeting, result.chapter);
  return new Response(png, {
    headers: {
      "Content-Type": "image/png",
    },
  });
};
