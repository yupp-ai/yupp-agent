/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    const ahs = (process.env.AHS_BASE_URL || 'http://localhost:8090').replace(/\/$/, '');
    const key = encodeURIComponent(process.env.AHS_API_KEY || '');
    return {
      // WebSocket proxy. Next.js route handlers can't accept WS upgrades,
      // so this rewrite (in beforeFiles) runs ahead of the catch-all route
      // handler at /api/ahs/[...path]/route.ts and forwards the upgrade
      // to AHS with the API key injected as a query param from server-
      // side env. Browser never sees the key.
      beforeFiles: [
        {
          source: '/api/ahs/session/:id/ws',
          destination: `${ahs}/ahs/session/:id/ws?api_key=${key}`,
        },
      ],
      afterFiles: [
        { source: '/api/healthz', destination: `${ahs}/healthz` },
      ],
      fallback: [],
    };
  },
};

export default nextConfig;
