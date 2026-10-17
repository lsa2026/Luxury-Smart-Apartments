/* Read-only progressive enhancement. All branch requests use the same owner gate. */
(() => {
  'use strict';
  const tree = document.querySelector('[data-loyalty-tree]');
  if (!tree) return;
  const status = tree.querySelector('[data-tree-status]');
  const activate = (root) => root.querySelectorAll('[data-tree-toggle], [data-tree-collapse]').forEach((button) => { button.hidden = false; });
  const targetFor = (button) => document.getElementById(button.getAttribute('aria-controls'));
  const setExpanded = (button, expanded) => {
    const target = targetFor(button);
    if (!target) return;
    target.hidden = !expanded;
    button.setAttribute('aria-expanded', String(expanded));
    button.textContent = expanded ? 'طي المدعوين' : 'عرض المدعوين';
  };
  const fragment = async (url) => {
    const address = new URL(url, window.location.href);
    address.searchParams.set('fragment', 'children');
    if (address.origin !== window.location.origin) throw new Error('origin');
    const response = await fetch(address, { credentials: 'same-origin', cache: 'no-store' });
    if (!response.ok || response.redirected || response.headers.get('X-Loyalty-Tree-Fragment') !== 'children') throw new Error('branch');
    const template = document.createElement('template');
    template.innerHTML = await response.text();
    return template.content;
  };
  activate(tree);
  tree.addEventListener('click', async (event) => {
    const toggle = event.target.closest('[data-tree-toggle]');
    if (toggle && tree.contains(toggle)) {
      if (toggle.disabled) return;
      const target = targetFor(toggle);
      if (!target) return;
      if (toggle.getAttribute('aria-expanded') === 'true') { setExpanded(toggle, false); return; }
      if (target.dataset.loaded) { setExpanded(toggle, true); return; }
      toggle.disabled = true;
      toggle.setAttribute('aria-busy', 'true');
      status.textContent = 'جارٍ تحميل المدعوين…';
      try {
        target.replaceChildren(await fragment(toggle.dataset.treeUrl));
        target.dataset.loaded = 'true';
        activate(target);
        setExpanded(toggle, true);
        status.textContent = 'تم عرض المدعوين. يمكنك فتح الفروع التالية.';
      } catch {
        status.textContent = 'تعذر تحميل الفرع. أعد المحاولة، أو حدّث الصفحة إذا انتهت جلسة الدخول.';
      } finally { toggle.disabled = false; toggle.removeAttribute('aria-busy'); }
      return;
    }
    const more = event.target.closest('[data-tree-more]');
    if (more && tree.contains(more)) {
      event.preventDefault();
      if (more.getAttribute('aria-busy') === 'true') return;
      more.setAttribute('aria-busy', 'true');
      status.textContent = 'جارٍ تحميل المزيد…';
      try {
        const content = await fragment(more.href);
        const container = more.parentElement;
        const nextList = content.querySelector('.loyalty-forest');
        container.querySelector(':scope > .loyalty-forest').append(...nextList.children);
        const nextMore = content.querySelector('[data-tree-more]');
        if (nextMore) more.replaceWith(nextMore); else more.remove();
        activate(container);
        status.textContent = 'تم عرض المزيد من الأعضاء.';
        // The old link may have been removed; keep keyboard focus in this branch.
        const nextTarget = nextMore || container.querySelector('.loyalty-branch:last-child a');
        if (nextTarget) nextTarget.focus({ preventScroll: true });
      } catch { more.removeAttribute('aria-busy'); status.textContent = 'تعذر تحميل المزيد. أعد المحاولة.'; }
      return;
    }
    if (event.target.closest('[data-tree-collapse]')) {
      tree.querySelectorAll('[data-tree-toggle]').forEach((button) => setExpanded(button, false));
      status.textContent = 'تم طي الفروع؛ افتح الفرع الذي تريد مراجعته.';
    }
  });
  // A single-root programme opens its first level immediately; deeper levels stay bounded.
  if (tree.hasAttribute('data-tree-initial')) tree.querySelector('[data-tree-toggle]')?.click();
})();
