// Optional browser smoke test. Requires Playwright and Edge (or BROWSER_CHANNEL).
// Run against an explicitly started local DEMO, never a real pilot service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = path.resolve(process.env.MTTR_DEMO_DIRECTORY || 'artifacts/pilot-demo');
  const token = fs.readFileSync(path.join(root, 'operator.token'), 'utf8').trim();
  const control = fs.readFileSync(path.join(root, 'control.token'), 'utf8').trim();
  const base = 'http://127.0.0.1:8787';
  const session = await (await fetch(base + '/api/session', {headers:{Authorization:`Bearer ${token}`}})).json();
  assert.equal(session.mode, 'DEMO', 'UI smoke test only admits a DEMO service');
  const browser = await chromium.launch({channel:process.env.BROWSER_CHANNEL || 'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1050}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.locator('#token').fill(token);
    await page.getByRole('button', {name:'连接工作台'}).click();
    await page.locator('#detail h2').waitFor();
    await page.getByRole('button', {name:'我来接手'}).click();
    await page.locator('#reason').fill('UI smoke: checked scope, revision and rollback compatibility');
    await page.getByRole('button', {name:'接受建议（不执行）'}).click();
    await page.getByText('审核：', {exact:false}).waitFor();
    await page.screenshot({path:path.join(root, 'workbench-desktop.png'), fullPage:true});
    const recovered = await fetch('http://127.0.0.1:9191/recover', {
      method:'POST',headers:{Authorization:`Bearer ${control}`, 'Content-Type':'application/json'},body:'{}'});
    assert.equal(recovered.status, 200);
    await page.locator('#reason').fill('UI smoke: external DEMO rollback completed; begin independent verification');
    await page.locator('#target').fill('r41');
    await page.getByRole('button', {name:'记录已人工处置，开始验证'}).click();
    await page.getByText('恢复已通过独立观测验证', {exact:false}).waitFor({timeout:55000});
    assert.match(await page.locator('#measurement').innerText(), /演练事件/);
    await page.screenshot({path:path.join(root, 'workbench-recovered.png'), fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(root, 'workbench-mobile.png'), fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'No horizontal overflow at mobile width');
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('#error').innerText(), '');
    await page.getByRole('button', {name:'退出',exact:true}).click();
    await page.locator('#token').waitFor();
    console.log(JSON.stringify({desktop:'passed',mobile:'passed',browser_errors:errors,flow:'ack-review-manual-action-verify-logout',screenshots:root}));
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error);process.exit(1);});
