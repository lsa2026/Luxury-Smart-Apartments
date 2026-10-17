const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('static/js/loyalty-person-tree.js', 'utf8');
const api = import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function events(object = {}) {
  const handlers = {};
  object.addEventListener = (type, handler) => { handlers[type] = handler; };
  object.emit = (type, event = {}) => handlers[type]?.({preventDefault() {}, ...event});
  return object;
}

function setup() {
  const timers = new Map();
  const close = events({focus() {close.focused = true;}});
  const content = {replaceChildren(value) {content.value = value;}};
  const popup = events({hidden: true, style: {}, offsetWidth: 320, offsetHeight: 400,
    contains: target => target === close || target === content,
    querySelector: selector => selector === '[data-tree-popup-content]' ? content : close,
  });
  const tree = events();
  const page = events({activeElement: null});
  const win = {innerWidth: 1280, innerHeight: 800, clearTimeout: id => timers.delete(id), setTimeout: fn => {timers.set(1, fn); return 1;}};
  const person = {attrs: {}, focused: false,
    contains: target => target === person,
    closest: selector => selector === '[data-tree-person]' ? person : {querySelector: () => ({content: {cloneNode: () => ({safeProfile: true})}})},
    setAttribute: (key, value) => {person.attrs[key] = value;},
    getClientRects: () => [1],
    getBoundingClientRect: () => ({left: 600, right: 700, top: 250}),
    focus() {person.focused = true; tree.emit('focusin', {target: person});},
  };
  return {tree, popup, page, win, person, close, content, timers};
}

test('hover shows the matching person profile and allows moving into its popup', async () => {
  const {bindProfiles} = await api, env = setup();
  bindProfiles(env.tree, env.popup, env);
  env.tree.emit('pointerover', {target: env.person, pointerType: 'mouse'});
  assert.equal(env.popup.hidden, false);
  assert.equal(env.person.attrs['aria-expanded'], 'true');
  assert.deepEqual(env.content.value, {safeProfile: true});
  env.tree.emit('pointerout', {target: env.person});
  assert.equal(env.timers.size, 1);
  env.popup.emit('pointerenter');
  assert.equal(env.timers.size, 0);
  env.popup.emit('pointerleave');
  env.timers.get(1)();
  assert.equal(env.popup.hidden, true);
});

test('touch does not open on pointerover; tapping pins, outside tap dismisses', async () => {
  const {bindProfiles} = await api, env = setup();
  bindProfiles(env.tree, env.popup, env);
  env.tree.emit('pointerover', {target: env.person, pointerType: 'touch'});
  assert.equal(env.popup.hidden, true);
  env.tree.emit('click', {target: env.person, detail: 1});
  assert.equal(env.popup.hidden, false);
  env.popup.emit('pointerleave');
  assert.equal(env.timers.size, 0);
  env.page.emit('pointerdown', {target: {closest: () => null}});
  assert.equal(env.popup.hidden, true);
});

test('keyboard focus shows information; Escape dismisses without immediately reopening', async () => {
  const {bindProfiles} = await api, env = setup();
  bindProfiles(env.tree, env.popup, env);
  env.tree.emit('focusin', {target: env.person});
  assert.equal(env.popup.hidden, false);
  env.tree.emit('click', {target: env.person, detail: 0});
  assert.equal(env.close.focused, true);
  env.page.emit('keydown', {key: 'Escape'});
  assert.equal(env.popup.hidden, true);
  assert.equal(env.person.focused, true);
  assert.equal(env.person.attrs['aria-expanded'], 'false');
});

test('close button returns focus and opening another page with modifiers remains native', async () => {
  const {bindProfiles} = await api, env = setup();
  bindProfiles(env.tree, env.popup, env);
  env.tree.emit('click', {target: env.person, ctrlKey: true});
  assert.equal(env.popup.hidden, true);
  env.tree.emit('click', {target: env.person, detail: 1});
  env.close.emit('click');
  assert.equal(env.popup.hidden, true);
  assert.equal(env.person.focused, true);
});

test('popup placement stays within desktop and mobile viewports', async () => {
  const {profilePosition} = await api;
  for (const width of [320, 390, 1280]) for (const left of [0, width / 2, width - 30]) {
    const size = {width: Math.min(320, width - 24), height: 400};
    const point = profilePosition({left, right: left + 30, top: 700}, size, {width, height: 844});
    assert.ok(point.left >= 12 && point.left + size.width <= width - 12);
    assert.ok(point.top >= 12 && point.top + size.height <= 844 - 12);
  }
});

test('curved arrows connect toward left, right or centered children with finite geometry', async () => {
  const {arrowPath} = await api;
  const from = {x: 200, y: 10, width: 42, height: 74};
  for (const x of [30, 200, 400]) {
    const path = arrowPath(from, {x, y: 180, width: 42, height: 74});
    assert.match(path, /^M[\d.,-]+ C[\d., -]+$/);
    assert.ok(path.endsWith(`${x + 21},171`));
    assert.doesNotMatch(path, /NaN|Infinity/);
  }
  assert.doesNotMatch(source, /fetch\(|XMLHttpRequest|localStorage|sendBeacon|innerHTML/);
});

test('single-child arrow starts below the name and branch control', async () => {
  const {arrowPath} = await api;
  for (const x of [200, 200.0000001]) {
    const path = arrowPath({x: 200, y: 10, width: 42, height: 74, foot: 150}, {x, y: 200, width: 42, height: 74});
    assert.ok(path.startsWith('M221,150 '));
    assert.ok(path.endsWith(`${x + 21},191`));
  }
});
