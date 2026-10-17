import assert from 'node:assert/strict';

export default async function run(page, ui) {
  const checks = [];
  const errors = [];
  page.on('pageerror', e => errors.push(String(e)));
  page.on('response', r => { if (r.status() >= 400 && !r.url().endsWith('/thumbnail')) errors.push(r.status() + ' ' + r.url()); });

  // 注意：Patchright 的 page.evaluate 在隔离世界里，看不到页面全局对象；
  // 这里只断言 DOM（DOM 是各世界共享的）。
  await page.waitForSelector('#btn-similar', { timeout: 15000 });
  await page.locator('#btn-similar').click();
  await page.waitForFunction(() => !document.querySelector('#similar-modal').classList.contains('hidden'));
  checks.push('modal opens');

  // 直接沿用夹具里已经建好的索引：上传一张与库内完全相同的文件
  const path = 'H:/ImageDB/_isotest/browser_query.jpg';
  await page.locator('#similar-file').setInputFiles(path);
  await page.waitForFunction(() => document.querySelectorAll('.similar-card').length > 0, null, { timeout: 15000 });
  const first = await page.locator('.similar-card').first();
  const badge = await first.locator('.similar-badge').textContent();
  assert.equal(badge.trim(), '完全相同');
  checks.push('exact duplicate found for uploaded file');

  // 换成"视觉近似"模式 + 放松门槛，结果数量应只增不减
  const countExact = await page.locator('.similar-card').count();
  await page.locator('#similar-mode').selectOption('similar');
  await page.locator('#similar-threshold').fill('16');
  await page.locator('#similar-threshold').dispatchEvent('change');
  await page.waitForTimeout(1200);
  const countLoose = await page.locator('.similar-card').count();
  assert(countLoose >= countExact);
  checks.push('loose threshold returns at least as many (' + countExact + ' -> ' + countLoose + ')');

  // 门槛收紧到 0：近似结果应消失（只可能剩完全相同的那张，但 exact 模式才保证）
  await page.locator('#similar-threshold').fill('0');
  await page.locator('#similar-threshold').dispatchEvent('change');
  await page.waitForTimeout(1200);
  const countStrict = await page.locator('.similar-card').count();
  assert(countStrict <= countLoose);
  checks.push('strict threshold narrows results (' + countLoose + ' -> ' + countStrict + ')');

  // 索引状态文案要显示出来
  const state = await page.locator('#similar-index-state').textContent();
  assert(state.includes('索引'));
  checks.push('index status shown: ' + state.trim());

  // 关闭再打开不报错，且能再次搜索
  await page.locator('[data-close="similar-modal"]').first().click();
  await page.locator('#btn-similar').click();
  await page.locator('#similar-mode').selectOption('auto');
  await page.locator('#similar-file').setInputFiles(path);
  await page.waitForFunction(() => document.querySelectorAll('.similar-card').length > 0, null, { timeout: 15000 });
  checks.push('reopen and search again');

  assert.deepEqual(errors, []);
  return { checks, cards: await page.locator('.similar-card').count() };
}
