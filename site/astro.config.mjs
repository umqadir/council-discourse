import { defineConfig } from "astro/config";
import sitemap from "@astrojs/sitemap";

const site = "https://council-discourse.pages.dev";
const redirectedBodyList = new URL("/meetings/new-york-city-council/", site).href;

export default defineConfig({
  output: "static",
  site,
  integrations: [
    sitemap({
      filter(page) {
        const normalized = page.endsWith("/") ? page : `${page}/`;
        return normalized !== redirectedBodyList;
      },
    }),
  ],
});
