import { realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";

/** Only files inside the selected model folder, including after symlinks. */
export function resolvePlaygroundAsset(root: string, url: string): string | null {
  if (!url.startsWith("/avatar/")) return null;
  try {
    const modelRoot = realpathSync(root);
    const requested = decodeURIComponent(url.split("?")[0].slice("/avatar/".length));
    const file = realpathSync(resolve(modelRoot, requested));
    const inside = relative(modelRoot, file);
    if (!inside || inside === ".." || inside.startsWith(`..${sep}`) || isAbsolute(inside)) {
      return null;
    }
    return statSync(file).isFile() ? file : null;
  } catch {
    return null;
  }
}
