"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { chromium } = require("playwright");

async function main() {
  const base = "http://127.0.0.1:8000";
  const artifacts = process.env.HOUSE_BILLS_BROWSER_ARTIFACT_DIR;
  if (!artifacts || process.env.HOUSE_BILLS_BROWSER_CHECK !== "true") {
    throw new Error("Use python -m tools.run_browser on disposable storage.");
  }
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const outbound = [];
  await context.route("**/*", route => {
    if (new URL(route.request().url()).origin === base) return route.continue();
    outbound.push(route.request().url());
    return route.abort();
  });
  await context.tracing.start({ screenshots: true, snapshots: true, sources: true });
  try {
    await page.goto(base);
    // The same trusted repository scenario remains usable with playwright-cli run-code.
    const filename = path.join(__dirname, "review.js");
    const scenario = vm.runInThisContext(`(${fs.readFileSync(filename, "utf8")})`, { filename });
    console.log(await scenario(page, artifacts));
    if (outbound.length) throw new Error(`Unexpected outbound requests: ${outbound.join(", ")}`);
  } catch (error) {
    await page.screenshot({ path: path.join(artifacts, "failure.png"), fullPage: true });
    throw error;
  } finally {
    await context.tracing.stop({ path: path.join(artifacts, "trace.zip") });
    await browser.close();
  }
}

main().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
