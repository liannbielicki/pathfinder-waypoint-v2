import type { NextConfig } from "next";

// The browser only ever talks to /api/*; this rewrite is the single committed
// route to the Railway origin. API_BASE_URL is the one non-secret Vercel value.
const nextConfig: NextConfig = {
  async rewrites() {
    const target = process.env.API_BASE_URL ?? "http://localhost:8000";
    return [{ source: "/api/:path*", destination: `${target}/api/:path*` }];
  },
};

export default nextConfig;
