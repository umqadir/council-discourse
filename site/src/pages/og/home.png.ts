import type { APIRoute } from "astro";
import { homeCard } from "@lib/og";

export const GET: APIRoute = async () => {
  const png = await homeCard();
  return new Response(new Uint8Array(png), {
    headers: { "Content-Type": "image/png" },
  });
};
