import process from "node:process";

const keys = ["RPA_PORTAL_URL", "RPA_USERNAME", "RPA_PASSWORD", "RPA_FORM_ID"];
const dryRun = process.argv.includes("--dry-run");
const config = Object.fromEntries(keys.map((key) => [key, process.env[key] ?? ""]));

if (dryRun) {
  console.log(JSON.stringify({ mode: "dry-run", configured: Boolean(config.RPA_PORTAL_URL), formIdPresent: Boolean(config.RPA_FORM_ID) }));
} else {
  const missing = keys.filter((key) => !config[key]);
  if (missing.length) throw new Error(`missing RPA configuration: ${missing.join(", ")}`);
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    await page.goto(config.RPA_PORTAL_URL, { waitUntil: "domcontentloaded" });
    await page.getByLabel("Username").fill(config.RPA_USERNAME);
    await page.getByLabel("Password").fill(config.RPA_PASSWORD);
    await page.getByRole("button", { name: /sign in|login/i }).click();
    await page.getByLabel("Form ID").fill(config.RPA_FORM_ID);
    await page.getByRole("button", { name: /submit|save/i }).click();
    console.log(JSON.stringify({ state: "submitted", formId: config.RPA_FORM_ID }));
  } finally {
    await browser.close();
  }
}
