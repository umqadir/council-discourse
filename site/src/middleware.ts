import { defineMiddleware } from "astro:middleware";
import {
  getChapterRouteRecordForPath,
  getRuntime,
  type CloudflareRuntime,
} from "@lib/edge-data";

const CACHE_CONTROL_CLIENT = "public, max-age=300, stale-while-revalidate=86400";
const CACHE_CONTROL_EDGE = "public, max-age=31536000, immutable";

export const onRequest = defineMiddleware(async (context, next) => {
  if (context.request.method !== "GET") {
    return next();
  }

  const url = new URL(context.request.url);
  const record = getChapterRouteRecordForPath(url.pathname);
  if (!record) {
    return next();
  }

  const runtime = getRuntime(context.locals);

  // Static-recent window: chapter pages and OG images for the newest meetings
  // are prerendered into the deploy as static assets at these same URLs.
  // _routes.json cannot carve them out per meeting (rules cap at 100
  // characters each and 100 entries total), so the worker runs for every
  // chapter path and serves the prerendered asset when one exists; only older
  // chapters fall through to the SSR render below.
  const assets = runtime?.env?.ASSETS;
  if (assets) {
    try {
      const asset = await assets.fetch(context.request.url);
      if (asset.ok) {
        const response = withClientCache(asset);
        response.headers.set("X-Static-Window", "HIT");
        return response;
      }
    } catch {
      // Fall through to the SSR render.
    }
  }

  const cache = runtime?.caches?.default ?? globalThis.caches?.default;
  if (!cache) {
    return withClientCache(await next());
  }

  const cacheKeyUrl = new URL(url.pathname, url.origin);
  cacheKeyUrl.searchParams.set("v", record.dataVersion);
  const cacheKey = new Request(cacheKeyUrl, context.request);
  const cached = await cache.match(cacheKey);
  if (cached) {
    return withCacheHeaders(cached, "HIT");
  }

  const response = await next();
  if (!response.ok) {
    return response;
  }

  const cacheResponse = withEdgeCache(response.clone());
  waitUntil(runtime, cache.put(cacheKey, cacheResponse));
  return withCacheHeaders(response, "MISS");
});

function withCacheHeaders(response: Response, status: "HIT" | "MISS"): Response {
  const nextResponse = withClientCache(response);
  nextResponse.headers.set("X-Edge-Cache", status);
  return nextResponse;
}

function withClientCache(response: Response): Response {
  const nextResponse = new Response(response.body, response);
  nextResponse.headers.set("Cache-Control", CACHE_CONTROL_CLIENT);
  return nextResponse;
}

function withEdgeCache(response: Response): Response {
  const cacheResponse = new Response(response.body, response);
  cacheResponse.headers.set("Cache-Control", CACHE_CONTROL_EDGE);
  return cacheResponse;
}

function waitUntil(runtime: CloudflareRuntime | undefined, promise: Promise<unknown>): void {
  if (runtime?.ctx?.waitUntil) {
    runtime.ctx.waitUntil(promise);
    return;
  }
  promise.catch(() => undefined);
}
