// Vite's settings. Vite = the dev server + build tool for the frontend.
//   npm run dev   -> Vite serves the site at http://localhost:5173 and reloads the browser
//                    instantly whenever you save a file.
//   npm run build -> tsc type-checks everything, then Vite bundles all the .tsx files into
//                    a few small static files in dist/ (that's what Vercel hosts).
// The react plugin teaches Vite to understand JSX and enables fast refresh.
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
});
