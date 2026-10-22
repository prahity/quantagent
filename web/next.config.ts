import type { NextConfig } from "next";

const SIM_API_URL = process.env.SIM_API_URL ?? "http://127.0.0.1:8711";

const nextConfig: NextConfig = {
  cacheComponents: true,
  partialPrefetching: true,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${SIM_API_URL}/api/:path*` }];
  },
};

export default nextConfig;
