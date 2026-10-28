/* Local HTML only: outbound requests are blocked before opening the preview. */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");
const assert = require("node:assert/strict");
let browser;

(async () => {
  const root = path.resolve(__dirname, "..");
  browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 }, deviceScaleFactor: 1 });
  const errors = [], external = [], checks = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route(/^https?:/, route => {
    external.push(route.request().url());
    return route.abort();
  });
  await page.goto(pathToFileURL(path.join(root, "artifacts/preview.html")).href);
  const finalFrame = async () => {
    await page.locator("#scrub").evaluate(el => {
      el.value = el.max;
      el.dispatchEvent(new Event("input", { bubbles: true }));
    });
  };
  const noOverflow = async () => {
    const bounds = await page.evaluate(() => ({
      content: document.documentElement.scrollWidth, viewport: innerWidth
    }));
    assert.ok(bounds.content <= bounds.viewport, JSON.stringify(bounds));
    const footers = await page.locator(".run-metrics").evaluateAll(nodes => nodes.map(el => ({
      width: el.clientWidth, content: el.scrollWidth,
      last: el === el.parentElement.lastElementChild,
      mixed: !!el.closest(".answer"),
      present: ["已耗时", "Token", "输出均速", "tok/s"].every(x => el.textContent.includes(x))
    })));
    for (const f of footers) assert.ok(f.width >= f.content && f.last && !f.mixed && f.present, JSON.stringify(f));
  };
  const scenarioCount = await page.locator(".scenario").count();
  assert.equal(scenarioCount, 10);
  let framesChecked = 0;
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1080 });
    for (let i = 0; i < scenarioCount; i++) {
      await page.locator(".scenario").nth(i).click();
      const max = Number(await page.locator("#scrub").getAttribute("max"));
      for (let j = 0; j <= max; j++) {
        await page.locator("#scrub").evaluate((el, value) => {
          el.value = value; el.dispatchEvent(new Event("input", { bubbles: true }));
        }, j);
        await noOverflow(); framesChecked++;
      }
      assert.ok(await page.locator(".answer,.plain-answer,.session-button").count());
    }
  }
  checks.push("ten scenarios / every frame at 320, 390, 768 and 1440 px without page overflow");
  await page.locator(".scenario").nth(1).click();
  await finalFrame();
  assert.equal(await page.locator(".card").count(), 2);
  await page.locator(".process summary").first().click();
  assert.equal(await page.locator(".process").first().getAttribute("open"), "");
  await page.screenshot({ path: path.join(root, "artifacts/preview-desktop.png"), fullPage: true });
  checks.push("main and continuation remain separate; process disclosure works");
  await page.locator(".scenario").nth(2).click();
  await finalFrame();
  assert.equal(await page.locator("#send-count").textContent(), "1");
  checks.push("lost ACK scenario ends with one committed reply");
  await page.locator(".scenario").nth(3).click();
  await finalFrame();
  assert.ok(!(await page.locator("#cards").innerText()).includes("迟到内容"));
  checks.push("late output is absent after cancellation");
  await page.locator(".scenario").nth(5).click();
  assert.ok((await page.locator(".status").innerText()).includes("未完成"));
  assert.equal(await page.locator(".card").count(), 1);
  await page.locator(".scenario").nth(6).click();
  const pieces = await page.locator(".plain-answer").allTextContents();
  assert.ok(pieces.length > 1);
  assert.ok(pieces.join("").endsWith("全文结束 · END"));
  const expected = await page.evaluate(() => JSON.parse(document.querySelector("#preview-data").textContent).scenarios[6].frames.at(-1).texts.join(""));
  assert.equal(pieces.join(""), expected);
  checks.push("error remains one card; long answer preserves every chunk and tail");
  await page.locator("#reset").click();
  assert.equal(await page.locator("#play").textContent(), "暂停");
  await page.locator("#play").click();
  assert.equal(await page.locator("#play").textContent(), "播放");
  await page.locator("#reduce").check();
  assert.ok(await page.locator("#play").isDisabled());
  await page.locator("#reset").click();
  assert.equal(await page.locator("#scrub").inputValue(), "0");
  checks.push("play, pause, replay, scrubbing and reduced motion work");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator(".scenario").nth(4).click();
  assert.equal(await page.locator(".answer table").count(), 1);
  assert.equal(await page.locator(".answer pre").count(), 1);
  await page.locator('[data-width="mobile"]').click();
  await noOverflow();
  await page.screenshot({ path: path.join(root, "artifacts/preview-mobile.png"), fullPage: true });
  checks.push("mobile code and table render; width control works");
  await page.locator("#theme").click();
  assert.equal(await page.locator("#theme").getAttribute("aria-pressed"), "true");
  await noOverflow();
  await page.screenshot({ path: path.join(root, "artifacts/preview-dark.png"), fullPage: true });
  // Clear the manual preference so disabled state proves the media change
  // was handled, rather than passing on an earlier checkbox selection.
  await page.locator("#reduce").uncheck();
  assert.ok(await page.locator("#play").isEnabled());
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.waitForFunction(() => document.querySelector("#reduce").checked && document.querySelector("#play").disabled);
  checks.push("dark preview and system reduced-motion preference work");
  await page.emulateMedia({ reducedMotion: "no-preference" });
  // matchMedia change is asynchronous; wait for its UI update before the
  // next interaction instead of racing it with a checkbox click.
  await page.waitForFunction(() => !document.querySelector("#reduce").checked);
  await page.locator(".scenario").nth(8).click();
  const stats = await page.locator(".run-metrics > summary").innerText();
  assert.ok(stats.includes("36秒"));
  assert.ok(stats.includes("deepseek-flash") && stats.includes("30.6 tok/s") && stats.includes("8,760"));
  await page.locator(".run-metrics").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(root, "artifacts/preview-metrics-mobile.png"), fullPage: true });
  await page.locator(".card").screenshot({ path: path.join(root, "artifacts/footer-mobile.png") });
  const first = await page.evaluate(() => { frameIndex = 1; draw(true, true); return document.querySelector(".answer").innerText; });
  await page.waitForFunction(initial => document.querySelector(".answer").innerText.length > initial.length, first);
  const next = await page.locator(".answer").innerText();
  assert.ok(next.length > first.length);
  await page.waitForFunction(() => document.querySelector(".answer").innerText === document.querySelector(".answer").dataset.full);
  await page.evaluate(() => { frameIndex = 1; draw(true, true); });
  await page.locator("#reduce").check();
  assert.equal(await page.locator(".answer").innerText(), await page.locator(".answer").getAttribute("data-full"));
  checks.push("reported usage totals; progressive typewriter; reduced motion immediately reveals full text");
  await page.locator(".scenario").nth(9).click();
  await page.getByRole("button", { name: "继续 · 方案讨论", exact: true }).click();
  assert.ok(await page.getByRole("button", { name: "当前 · 方案讨论", exact: true }).isDisabled());
  await page.getByRole("button", { name: "新建当前会话", exact: true }).click();
  assert.ok(await page.getByRole("button", { name: "当前 · 新会话", exact: true }).isDisabled());
  await page.getByRole("button", { name: "另开独立话题", exact: true }).click();
  assert.equal(await page.locator(".card").count(), 3);
  assert.ok(await page.getByRole("button", { name: "新建当前会话", exact: true }).isDisabled());
  await page.getByRole("button", { name: "刷新会话列表", exact: true }).click();
  await page.getByRole("button", { name: "打字机：开 · 点击关闭", exact: true }).click();
  assert.equal(await page.getByRole("button", { name: "打字机：关 · 点击开启", exact: true }).count(), 1);
  await noOverflow();
  await page.screenshot({ path: path.join(root, "artifacts/preview-sessions-mobile.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1080 });
  await page.locator("#theme").click();
  await page.locator('[data-width="desktop"]').click();
  await page.screenshot({ path: path.join(root, "artifacts/preview-sessions-desktop.png"), fullPage: true });
  await page.locator(".scenario").nth(8).click();
  await page.locator(".card").screenshot({ path: path.join(root, "artifacts/footer-desktop.png") });
  await page.locator(".process > summary").click();
  await page.locator(".loop-detail > summary").filter({ hasText: "核对参考资料" }).click();
  const detail = await page.locator(".process > .steps").innerText();
  assert.ok(detail.includes("核对参考资料") && detail.includes("✓ 完成"));
  assert.ok(!detail.includes("0.0s") && !detail.includes("· 已完成"));
  const markers = await page.locator('.process summary').evaluateAll(nodes => nodes.map(el => ({
    before: getComputedStyle(el, '::before').content,
    after: getComputedStyle(el, '::after').content
  })));
  assert.ok(markers.every(m => ['none', 'normal', '""'].includes(m.before) && m.after !== 'none'));
  await page.locator(".loop-detail > summary").filter({ hasText: "用量与耗时依据" }).click();
  const facts = await page.locator(".process > .steps").innerText();
  assert.ok(facts.includes("Token 输入") && facts.includes("耗时参考已积累"));
  await page.locator(".card").screenshot({ path: path.join(root, "artifacts/footer-expanded-desktop.png") });
  checks.push("footer remains last and separate in every streaming/terminal frame; model and reported end-to-end output rate; no footer overflow");
  checks.push("offline session panel switches, creates, opens parallel topics and toggles typing; busy switch disabled");
  assert.deepEqual(errors, []);
  assert.deepEqual(external, []);
  const report = { passed: true, checks, browser_errors: errors, external_requests: external,
    viewports: [320, 390, 768, 1440], frames_checked: framesChecked,
    note: "Local HTML simulation only; not Feishu client rendering acceptance." };
  fs.writeFileSync(path.join(root, "artifacts/ui-verification.json"), JSON.stringify(report, null, 2) + "\n");
  await browser.close();
  console.log(JSON.stringify(report, null, 2));
})().catch(async error => {
  console.error(error);
  if (browser) await browser.close();
  process.exitCode = 1;
});
