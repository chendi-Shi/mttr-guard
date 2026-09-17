// Invoked by the isolated enterprise drill, never against a production service.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const base = process.env.MTTR_UI_BASE, token = process.env.MTTR_UI_TOKEN;
  assert.equal(new URL(base).hostname, '127.0.0.1');
  const headers = {Authorization:`Bearer ${token}`};
  const session = await (await fetch(base + '/api/session', {headers})).json();
  assert.equal(session.mode, 'DEMO');
  const incident = async () => (await (await fetch(base + '/api/incidents/' + process.env.MTTR_UI_INCIDENT, {headers})).json()).incident;
  const browser = await chromium.launch({channel:process.env.BROWSER_CHANNEL || 'msedge', headless:true});
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1050}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base);
    await page.locator('#token').fill(token);
    await page.getByRole('button', {name:'连接工作台'}).click();
    await page.locator('#pause-reason').fill('DEMO: verify manual takeover');
    await page.locator('#pause').click();
    await page.locator('#reason').waitFor();
    assert.equal((await incident()).state, 'NEEDS_ATTENTION');
    await page.locator('#assignee').selectOption(session.subject);
    await page.locator('#handoff-reason').fill('DEMO: verify on-call handoff');
    const assigned = page.waitForResponse(r => r.url().endsWith('/handoff') && r.request().method() === 'POST');
    await page.locator('#handoff').click();
    assert.equal((await assigned).status(), 200);
    await page.getByText('接手人：' + session.subject, {exact:true}).waitFor();
    assert.equal((await incident()).assignment.subject, session.subject);
    fs.mkdirSync('artifacts/enterprise-ui', {recursive:true});
    await page.screenshot({path:'artifacts/enterprise-ui/desktop.png',fullPage:true});
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:'artifacts/enterprise-ui/mobile.png',fullPage:true});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#reason').fill('DEMO: resume recovery verification after handoff');
    await page.locator('#target').fill('r41');
    await page.locator('#observe').click();
    await page.locator('#pause').waitFor();
    assert.equal((await incident()).state, 'OBSERVING');
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('#error').innerText(), '');
    assert.equal(await page.evaluate(() => localStorage.length + sessionStorage.length), 0);
    await page.getByRole('button', {name:'退出',exact:true}).click();
    await page.locator('#token').waitFor();
    console.log(JSON.stringify({browser:'passed',desktop:1440,mobile:390,flow:'login-pause-handoff-resume-logout'}));
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exit(1);});
