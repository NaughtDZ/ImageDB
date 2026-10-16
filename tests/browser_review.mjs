import assert from 'node:assert/strict';

export default async function run(page, ui) {
  const checks = [];
  const unexpectedNetwork = [], scriptErrors = [];
  page.on('pageerror', error => scriptErrors.push(String(error)));
  page.on('response', response => { if (response.status() >= 400) unexpectedNetwork.push(response.status() + ' ' + response.url()); });
  page.on('requestfailed', request => {
    const reason = request.failure()?.errorText || '';
    if (!(reason.includes('ERR_ABORTED') && request.url().endsWith('/thumbnail'))) unexpectedNetwork.push(reason + ' ' + request.url());
  });
  // Patchright's evaluate uses an isolated world; CDP targets the page's main
  // world so these assertions inspect the real application state, not a mock.
  const cdp = await page.context().newCDPSession(page);
  const main = async fn => {
    const result = await cdp.send('Runtime.evaluate', {
      expression: '(' + fn.toString() + ')()', awaitPromise: true, returnByValue: true,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  };
  const snapshot = await ui.snapshot();
  assert(snapshot.includes('搜索') && snapshot.includes('标签'), 'expected fixture UI controls');
  await page.waitForFunction(() => App.state.items.length > 0);
  // Search on a later page must request page 1, not just relabel the old response.
  await main(async () => { App.state.page = 3; await Gallery.load(false); });
  await page.locator('#search-q').fill('beta-');
  await page.locator('#btn-search').click();
  await page.waitForFunction(() => App.state.total === 5 && App.state.page === 1 && App.state.items.length === 5);
  checks.push('page reset precedes filtered request');

  // Hold a real old API request, complete the newer real request first.
  let releaseOld, oldStartedResolve;
  const oldStarted = new Promise(resolve => { oldStartedResolve = resolve; });
  const oldHeld = new Promise(resolve => { releaseOld = resolve; });
  await page.route('**/api/media?**', async route => {
    const url = new URL(route.request().url());
    if (url.searchParams.get('q') === 'alpha-') { oldStartedResolve(); await oldHeld; }
    await route.continue();
  });
  await main(() => { App.state.filters.q = 'alpha-'; window.qaOldRequest = Gallery.load(true); });
  await oldStarted;
  await main(async () => { App.state.filters.q = 'beta-'; await Gallery.load(true); });
  releaseOld();
  await main(async () => { await window.qaOldRequest; });
  assert.deepEqual(await main(() => ({q: App.state.filters.q, count: App.state.total, allBeta: App.state.items.every(x => x.filename.startsWith('beta-'))})),
    {q: 'beta-', count: 5, allBeta: true});
  await page.unroute('**/api/media?**');
  checks.push('late old list response cannot replace current filter');

  // Selection changes CSS, never detaches the original thumbnail/card DOM.
  await main(() => { window.qaCard = document.querySelector('.media-card'); window.qaImg = qaCard.querySelector('.thumb-img'); });
  await page.locator('.media-card').first().click();
  assert(await main(() => document.querySelector('.media-card') === qaCard && qaCard.querySelector('.thumb-img') === qaImg && App.state.selected.size === 1));
  await page.waitForFunction(() => document.querySelector('.media-card .thumb-img').dataset.loaded === '1');
  checks.push('selection retains card and thumbnail nodes');

  await page.locator('#btn-manual-tags').click();
  await page.waitForFunction(() => !document.querySelector('#tags-modal').classList.contains('hidden') && document.querySelector('#tags-list').textContent !== '读取中…');
  await page.locator('#tags-new').fill('review-manual-tag');
  await page.locator('#tags-add').click();
  await page.waitForFunction(() => !TagsEditor._saving && TagsEditor.currentTags.has('review-manual-tag'));
  assert.equal(await page.locator('#tags-list .tag-chip').count(), 1);
  assert(await main(() => App.state.selected.size === 1 && App.state.items.find(x => x.id === TagsEditor.ids[0]).tags.some(x => x.name === 'review-manual-tag')));
  await page.locator('#tags-list .tag-chip .del').click();
  await page.waitForFunction(() => !TagsEditor._saving && !TagsEditor.currentTags.has('review-manual-tag'));
  assert.equal(await page.locator('#tags-list .tag-chip').count(), 0);
  await page.locator('[data-close="tags-modal"]').first().click();
  checks.push('manual tag add/remove refreshes editor and preserves selection');

  // Start a real, model-free metadata task, close/reopen while start HTTP is held.
  let releaseStart, startedResolve;
  const startSeen = new Promise(resolve => { startedResolve = resolve; });
  const startHeld = new Promise(resolve => { releaseStart = resolve; });
  await page.route('**/api/tagging/run', async route => { startedResolve(); await startHeld; await route.continue(); });
  await page.locator('#btn-tag').click();
  await page.locator('#tagger-tools .tool-option[data-tool="metadata"]').click();
  await page.locator('#tagger-start').click();
  await startSeen;
  await page.locator('[data-close="tagger-modal"]').first().click();
  await page.locator('#btn-tag').click();
  await page.waitForFunction(() => !document.querySelector('#tagger-modal').classList.contains('hidden'));
  assert(await page.locator('#tagger-start').isDisabled());
  releaseStart();
  await page.waitForFunction(() => !Tagger._starting && !Tagger.jobId && !document.querySelector('#tagger-start').disabled);
  await page.unroute('**/api/tagging/run');
  await page.locator('[data-close="tagger-modal"]').first().click();
  checks.push('close/reopen during tagging start reconnects and unlocks at completion');

  await page.locator('#page-size-input').fill('500');
  await page.locator('#page-size-input').dispatchEvent('change');
  await page.waitForFunction(() => App.state.pageSize === 200 && document.querySelector('#page-size-input').value === '200');
  const requests = [];
  const onRequest = req => { if (new URL(req.url()).pathname === '/api/media') requests.push(req.url()); };
  page.on('request', onRequest);
  await page.reload();
  await page.waitForFunction(() => App.state.total === 135 && App.state.items.length === 135);
  page.off('request', onRequest);
  assert(requests.length > 0);
  assert.equal(new URL(requests[0]).searchParams.get('page_size'), '200');
  assert.equal(await page.locator('#page-size-input').getAttribute('max'), '200');
  checks.push('page-size upper bound and persisted first request agree with API');

  assert.deepEqual(scriptErrors, []);
  assert.deepEqual(unexpectedNetwork, []);
  return { checks, unexpectedNetwork, scriptErrors, finalItems: await page.locator('.media-card').count(), snapshotBefore: snapshot };
}
