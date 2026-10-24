/**
 * Package Version Utility
 *
 * Single source of truth for reading this package's version from package.json.
 * All other modules that need the version at runtime should import from here
 * instead of hardcoding a version string.
 */

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

let cached: string | undefined;

/**
 * Read this package's version from package.json.
 * Cached after first call — the version never changes at runtime.
 *
 * Returns `undefined` if package.json cannot be found or parsed
 * (e.g. in a bundled context without the file alongside).
 */
export function getPackageVersion(): string | undefined {
  if (cached !== undefined) return cached;
  try {
    const pkgPath = join(
      dirname(fileURLToPath(import.meta.url)),
      "..",
      "..",
      "package.json",
    );
    const parsed = JSON.parse(readFileSync(pkgPath, "utf-8")) as {
      version?: string;
    };
    cached =
      typeof parsed?.version === "string" ? parsed.version : undefined;
  } catch {
    cached = undefined;
  }
  return cached;
}
