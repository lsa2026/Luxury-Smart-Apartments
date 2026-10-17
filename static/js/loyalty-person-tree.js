/* Native, read-only diagram. No guest data is sent to third-party drawing services. */
export function arrowPath(from, to) {
  const direction = Math.sign(to.x + to.width / 2 - from.x - from.width / 2);
  const x = from.x + from.width / 2 + direction * (from.width / 2 + 9);
  const y = from.y + (direction ? from.height * .36 : from.height + 4);
  const endX = to.x + to.width / 2, endY = to.y - 9;
  const bend = Math.max(12, (endY - y) * .55);
  return `M${x},${y} C${x + direction * 24},${y + bend} ${endX},${endY - bend} ${endX},${endY}`;
}

export function profilePosition(anchor, size, viewport) {
  const gap = 12;
  const width = Math.min(size.width, viewport.width - gap * 2);
  const height = Math.min(size.height, viewport.height - gap * 2);
  const preferred = anchor.right + gap + width <= viewport.width ? anchor.right + gap : anchor.left - width - gap;
  return {
    left: Math.max(gap, Math.min(preferred, viewport.width - width - gap)),
    top: viewport.width < 600 ? Math.max(gap, viewport.height - height - gap) : Math.max(gap, Math.min(anchor.top - 20, viewport.height - height - gap)),
  };
}

export function bindProfiles(tree, popup, {page = document, win = window} = {}) {
  const content = popup.querySelector('[data-tree-popup-content]');
  const closeButton = popup.querySelector('[data-tree-popup-close]');
  let active = null, pinned = false, timer = null, suppressed = null;
  const keep = () => { win.clearTimeout(timer); };
  const place = () => {
    if (!active || popup.hidden) return;
    if (!active.getClientRects().length) { close(); return; }
    const point = profilePosition(active.getBoundingClientRect(), {width: popup.offsetWidth, height: popup.offsetHeight}, {width: win.innerWidth, height: win.innerHeight});
    popup.style.left = `${point.left}px`;
    popup.style.top = `${point.top}px`;
  };
  const close = (restore = false) => {
    keep();
    const previous = active;
    if (active) active.setAttribute('aria-expanded', 'false');
    active = null; pinned = false; popup.hidden = true;
    if (restore && previous) { suppressed = previous; previous.focus({preventScroll: true}); }
  };
  const show = (person, pin = false, force = false) => {
    if (!person || suppressed === person || (pinned && !pin && !force)) return;
    const profile = person.closest('.loyalty-person').querySelector('[data-tree-profile]');
    if (!profile) return;
    keep();
    if (active !== person) {
      if (active) active.setAttribute('aria-expanded', 'false');
      content.replaceChildren(profile.content.cloneNode(true));
      active = person;
    }
    pinned = pin;
    popup.hidden = false;
    person.setAttribute('aria-expanded', 'true');
    place();
  };
  const delayClose = () => {
    keep();
    if (!pinned && !popup.contains(page.activeElement) && page.activeElement !== active) timer = win.setTimeout(() => close(), 220);
  };
  const personFor = (target) => target?.closest?.('[data-tree-person]');
  tree.addEventListener('pointerover', event => {
    if (event.pointerType === 'touch') return;
    const person = personFor(event.target);
    if (person && !person.contains(event.relatedTarget)) show(person);
  });
  tree.addEventListener('pointerout', event => {
    const person = personFor(event.target);
    if (!person || person.contains(event.relatedTarget)) return;
    if (suppressed === person) suppressed = null;
    if (!popup.contains(event.relatedTarget)) delayClose();
  });
  tree.addEventListener('focusin', event => {
    const person = personFor(event.target);
    if (person) show(person, false, true);
  });
  tree.addEventListener('focusout', event => {
    if (!popup.contains(event.relatedTarget) && event.relatedTarget !== active) delayClose();
  });
  tree.addEventListener('click', event => {
    const person = personFor(event.target);
    if (!person || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    suppressed = null;
    if (active === person && pinned) close(); else show(person, true, true);
    if (event.detail === 0 && !popup.hidden) closeButton.focus();
  });
  tree.addEventListener('keydown', event => {
    if (event.key === ' ' && personFor(event.target)) { event.preventDefault(); event.target.click(); }
  });
  popup.addEventListener('pointerenter', keep);
  popup.addEventListener('pointerleave', delayClose);
  popup.addEventListener('focusout', event => {
    if (!popup.contains(event.relatedTarget) && event.relatedTarget !== active) close();
  });
  closeButton.addEventListener('click', () => close(true));
  page.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !popup.hidden) {
      event.preventDefault(); suppressed = active; close(true);
    }
  });
  page.addEventListener('pointerdown', event => {
    if (!popup.hidden && !popup.contains(event.target) && !personFor(event.target)) close();
  });
  return {close, place};
}

export function bindPersonTree(tree, {page = document, win = window} = {}) {
  if (!tree) return;
  const canvas = tree.querySelector('[data-tree-canvas]');
  const stage = tree.querySelector('[data-tree-stage]');
  const space = tree.querySelector('[data-tree-space]');
  const wires = tree.querySelector('[data-tree-wires]');
  const popup = tree.querySelector('[data-tree-popup]');
  if (!canvas || !stage || !space || !wires || !popup) return;
  const profiles = bindProfiles(tree, popup, {page, win});
  const controls = tree.querySelector('[data-tree-zoom-controls]');
  const output = tree.querySelector('[data-tree-scale]');
  const number = new Intl.NumberFormat('ar-SA', {numberingSystem: 'arab', maximumFractionDigits: 0});
  let scale = 1, frame = null, initialized = false, drag = null;
  const dimensions = () => ({width: stage.offsetWidth, height: stage.offsetHeight});
  const applySize = () => {
    const natural = dimensions();
    const left = Math.max(16, (canvas.clientWidth - natural.width * scale) / 2);
    stage.style.left = `${left}px`;
    stage.style.top = '16px';
    stage.style.transform = `scale(${scale})`;
    space.style.width = `${Math.max(canvas.clientWidth, natural.width * scale + 32)}px`;
    space.style.height = `${Math.max(canvas.clientHeight, natural.height * scale + 32)}px`;
    output.textContent = `${number.format(scale * 100)}٪`;
  };
  const fit = () => {
    const natural = dimensions();
    scale = Math.max(.15, Math.min(1, (canvas.clientWidth - 32) / Math.max(1, natural.width), (canvas.clientHeight - 32) / Math.max(1, natural.height)));
  };
  const draw = () => {
    frame = null;
    const firstDraw = !initialized;
    if (!initialized) {
      canvas.dataset.enhanced = 'true';
      if (win.innerWidth > 767) fit();
      initialized = true;
    }
    applySize();
    if (firstDraw) canvas.scrollLeft = Math.max(0, (space.offsetWidth - canvas.clientWidth) / 2);
    const bounds = stage.getBoundingClientRect();
    const rect = icon => {
      const r = icon.getBoundingClientRect();
      return {x: (r.left - bounds.left) / scale, y: (r.top - bounds.top) / scale, width: r.width / scale, height: r.height / scale};
    };
    const paths = [];
    for (const branch of stage.querySelectorAll('.loyalty-branch')) {
      const target = branch.querySelector(':scope > .loyalty-person [data-person-icon]');
      if (!target || !target.getClientRects().length) continue;
      const parent = branch.parentElement.closest('.loyalty-branch');
      if (!parent) continue;
      const source = parent.querySelector(':scope > .loyalty-person [data-person-icon]');
      if (!source || !source.getClientRects().length) continue;
      const path = page.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', arrowPath(rect(source), rect(target)));
      paths.push(path);
    }
    wires.replaceChildren(...paths);
    profiles.place();
  };
  const redraw = () => { if (frame === null) frame = win.requestAnimationFrame(draw); };
  controls.hidden = false;
  tree.addEventListener('loyalty-tree-layout', redraw);
  win.addEventListener('resize', redraw);
  if (win.ResizeObserver) {
    const observer = new win.ResizeObserver(redraw);
    observer.observe(canvas); observer.observe(stage);
  }
  tree.addEventListener('click', event => {
    const button = event.target.closest('[data-tree-zoom]');
    if (!button) return;
    profiles.close();
    const old = scale, natural = dimensions();
    const oldLeft = Math.max(16, (canvas.clientWidth - natural.width * old) / 2);
    const center = (canvas.scrollLeft + canvas.clientWidth / 2 - oldLeft) / old;
    if (button.dataset.treeZoom === 'fit') fit();
    else if (button.dataset.treeZoom === 'reset') scale = 1;
    else scale = Math.max(.15, Math.min(2, scale * (button.dataset.treeZoom === 'in' ? 1.2 : 1 / 1.2)));
    applySize();
    const left = Math.max(16, (canvas.clientWidth - natural.width * scale) / 2);
    canvas.scrollLeft = center * scale + left - canvas.clientWidth / 2;
    redraw();
  });
  canvas.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.pointerType !== 'mouse' || event.target.closest('a,button')) return;
    profiles.close();
    drag = {id: event.pointerId, x: event.clientX, y: event.clientY, left: canvas.scrollLeft, top: canvas.scrollTop};
    canvas.setPointerCapture(event.pointerId);
    canvas.dataset.dragging = 'true';
  });
  canvas.addEventListener('pointermove', event => {
    if (!drag || event.pointerId !== drag.id) return;
    event.preventDefault();
    canvas.scrollLeft = drag.left - event.clientX + drag.x;
    canvas.scrollTop = drag.top - event.clientY + drag.y;
  });
  const stopDrag = () => { drag = null; delete canvas.dataset.dragging; };
  canvas.addEventListener('pointerup', stopDrag);
  canvas.addEventListener('pointercancel', stopDrag);
  canvas.addEventListener('lostpointercapture', stopDrag);
  canvas.addEventListener('scroll', () => profiles.place(), {passive: true});
  redraw();
}

if (typeof document !== 'undefined') bindPersonTree(document.querySelector('[data-loyalty-tree]'));
