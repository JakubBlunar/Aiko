import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolvePlaygroundAsset } from "./scripts/playgroundAssets";

const webRoot = dirname(fileURLToPath(import.meta.url));

export default defineConfig(() => {
  const modelRoot = process.env.AIKO_PLAYGROUND_MODEL;
  const profileFile = process.env.AIKO_PLAYGROUND_PROFILE;
  if (!modelRoot || !profileFile) throw new Error("Launch with npm run avatar:playground");
  const profile = readFileSync(profileFile, "utf8");
  return {
    root: webRoot,
    plugins: [react(), {
      name: "aiko-avatar-playground",
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.url?.split("?")[0] === "/playground/profile.json") {
            res.setHeader("Content-Type", "application/json; charset=utf-8");
            res.setHeader("Cache-Control", "no-store");
            res.end(profile);
            return;
          }
          if (req.url?.startsWith("/avatar/")) {
            const asset = resolvePlaygroundAsset(modelRoot, req.url);
            if (!asset) {
              res.statusCode = 404;
              res.end("Model asset not found");
              return;
            }
            req.url = "/@fs/" + encodeURI(asset.replaceAll("\\", "/"))
              .replaceAll("?", "%3F").replaceAll("#", "%23");
          }
          next();
        });
      },
    }],
    resolve: { alias: { "@": resolve(webRoot, "src") } },
    define: { __APP_BUILD_ID__: JSON.stringify("avatar-playground") },
    server: {
      host: "127.0.0.1",
      fs: { allow: [resolve(webRoot, ".."), modelRoot] },
    },
  };
});
