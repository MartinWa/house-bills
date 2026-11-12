"use strict";

const assert = require("node:assert/strict");
const { chromium } = require("playwright");

async function main() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ timezoneId: "Europe/Stockholm" });
  const page = await context.newPage();
  await page.goto("http://grafana:3000/login");
  await page.locator('input[name="user"]').fill("synthetic");
  await page.locator('input[name="password"]').fill("synthetic-grafana-check");
  await page.getByRole("button", { name: "Log in", exact: true }).click();
  await page.waitForURL(url => !url.pathname.endsWith("/login"));
  for (const [query, expectedCosts, expectedUsage] of [
    ["var-property=1", 551 * 125, 551 * 123],
    ["var-property=1&var-property=2", 551 * 125 + 550 * 250, 551 * 123 + 550 * 246],
    ["var-property=$__all", 551 * 125 + 550 * 250, 551 * 123 + 550 * 246],
    // January ends at 23:59:59 UTC, which is already February 1 in Stockholm.
    ["var-property=$__all&from=1767225600000&to=1769903999000", 551 * 125, 551 * 123 + 550 * 246],
  ]) {
    const responses = [];
    const collect = async response => {
      if (!response.url().includes("/api/ds/query")) return;
      const body = response.request().postDataJSON();
      const target = body?.queries?.find(target => target.url?.includes("/api/v1/reports/"));
      if (!target) return;
      responses.push({ target, status: response.status(), result: await response.json() });
    };
    page.on("response", collect);
    await page.goto(`http://grafana:3000/d/house-bills-api-check?${query}`);
    const deadline = Date.now() + 30000;
    while (responses.length < 2 && Date.now() < deadline) await page.waitForTimeout(250);
    page.off("response", collect);
    assert.equal(responses.length, 2, "Both approved-report panels must query successfully");
    for (const { target, status, result } of responses) {
      assert.equal(status, 200, JSON.stringify(result));
      const frames = result.results.A.frames;
      const costs = target.url.includes("/reports/costs");
      let sum = 0;
      for (const frame of frames) {
        assert.ok(frame.schema.fields.length, JSON.stringify(result));
        frame.schema.fields.forEach((field, index) => {
          if (field.type === "number") sum += frame.data.values[index].reduce((a, b) => a + b, 0);
          if (field.type === "time") {
            assert.ok(frame.data.values[index].every(value => [1767225600000, 1769904000000].includes(value)));
          }
        });
      }
      const total = costs ? expectedCosts / 100 : expectedUsage / 1000;
      assert.ok(Math.abs(sum - total) < 0.000001, `${target.url}: ${sum} != ${total}`);
    }
    await page.getByText("Approved costs by invoice month (SEK)", { exact: true }).waitFor();
    await page.getByText("Billed water by period end (m³)", { exact: true }).waitFor();
  }
  await browser.close();
  console.log("Grafana panels: exact costs/consumption, single/multiple/all-property selectors and UTC dates/range boundaries in a Stockholm browser passed.");
}

main().catch(error => { console.error(error); process.exit(1); });
