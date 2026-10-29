/* 中间机配置步骤条:按现有抽取计划 API 标注「扫描选表 / 抽取计划」完成情况。 */
(function () {
  'use strict';

  var stepper = document.getElementById('config-stepper');
  if (!stepper || document.body.dataset.needsSetup === 'true') return;
  if (document.body.dataset.needsToken === 'true' && !sessionStorage.getItem('d2a_token')) return;

  function mark(step, done, sub) {
    var link = stepper.querySelector('[data-step="' + step + '"]');
    if (!link) return;
    link.classList.toggle('done', done);
    var num = link.querySelector('.num');
    if (num) num.textContent = done ? '✓' : (step === 'metadata' ? '2' : '3');
    var subEl = link.querySelector('.sub');
    if (subEl && sub) subEl.textContent = sub;
  }

  async function refresh() {
    try {
      var plan = await apiJson('/api/extraction-tables', { timeoutMs: 10000 });
      var count = Object.keys(plan.tables || {}).length;
      mark('metadata', count > 0, count > 0 ? '已加入 ' + count + ' 张表' : '尚未选表');
      mark('tables', count > 0, count > 0 ? count + ' 张表' : '计划为空');
    } catch (_error) { /* 步骤条只做提示,失败时保持默认文案 */ }
  }

  refresh();
  // 选表/计划页保存后刷新步骤状态
  document.addEventListener('d2a:plan-saved', refresh);
})();
