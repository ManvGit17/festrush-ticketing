// Rerunnable: E2E_BASE_URL=http://localhost:8090 node screenshots.mjs  (needs a seeded, running app)
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const base = process.env.E2E_BASE_URL ?? "http://localhost:8080";
const out = "../docs/assets/";
const browser = await chromium.launch();
const page = await (await browser.newContext({ viewport: { width: 1100, height: 700 } })).newPage();
await page.goto(base + "/login");
await page.getByLabel("Username").fill("demo02");
await page.getByLabel("Password").fill(process.env.FESTRUSH_DEMO_PASSWORD ?? "festrush-demo");
await page.getByRole("button", { name: /sign in/i }).click();
await page.goto(base + "/events");
await page.screenshot({ path: out + "events.png" });
await page.goto(base + "/events/1/live");
await page.getByRole("button", { name: "Hold a seat" }).click();
await page.locator(".seat.mine").first().waitFor();
await page.getByRole("button", { name: "System Lens" }).click();
await page.locator("#lens dt").first().waitFor();
await page.screenshot({ path: out + "seat-map-and-lens.png" });

const logo = readFileSync(out + "logo.svg", "utf8");
const social = await (await browser.newContext({ viewport: { width: 1280, height: 640 } })).newPage();
await social.setContent(`<body style="margin:0;display:grid;place-items:center;height:100vh;background:#f6f5f1;
font-family:Arial,sans-serif"><div style="width:720px">${logo}</div>
<p style="position:absolute;bottom:150px;font-size:34px;color:#17181c">Ticketing that never sells a seat twice</p></body>`);
await social.screenshot({ path: out + "social-preview.png" });
await browser.close();
