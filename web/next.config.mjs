/** Static export: FastAPI serves web/out, so the demo is a single deployable. */
const nextConfig = { output: "export", trailingSlash: false, images: { unoptimized: true } };
export default nextConfig;
