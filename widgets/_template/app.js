/* DeskRobot widget runtime template.
 *
 * Available globals (provided by the iOS host):
 *   window.motem.locale          // "zh-Hans" | "en"
 *   window.motem.size            // "small" | "medium" | "large" | "xlarge"
 *   window.motem.params          // user-supplied static params (object)
 *   window.motem.data            // { weather, todos, pomodoro, time }
 *   window.motem.onTimeTick(cb)  // called every second with new Date()
 *   window.motem.onDataChange(cb)// called on weather/todos/pomodoro update
 *
 * Locale strings used inside the widget MUST be a dict with both en & zh-Hans.
 * Use deskrobot.locale to pick the right one.
 */

(function () {
  const root = document.getElementById('root');
  const titleEl = document.getElementById('title');
  const valueEl = document.getElementById('value');
  const captionEl = document.getElementById('caption');

  const STR = {
    title:   { en: 'CLOCK',           'zh-Hans': '时钟' },
    caption: { en: 'TAP TO RESTART',  'zh-Hans': '点击重启' },
  };
  function tr(key) {
    const dict = STR[key] || {};
    return dict[window.motem && window.motem.locale] || dict.en || key;
  }

  function format(date) {
    const hh = String(date.getHours()).padStart(2, '0');
    const mm = String(date.getMinutes()).padStart(2, '0');
    return `${hh}:${mm}`;
  }

  function render(date) {
    titleEl.textContent = tr('title');
    captionEl.textContent = tr('caption');
    valueEl.textContent = format(date);
  }

  // Initial paint with whatever data we have.
  render(new Date());

  // Subscribe to host-pushed time ticks (avoid setInterval — saves CPU when offscreen).
  if (window.motem && typeof window.motem.onTimeTick === 'function') {
    window.motem.onTimeTick(function (now) {
      render(now);
    });
  }

  // Tap-to-restart wires nothing meaningful in the template; AI can extend it.
  document.body.addEventListener('click', function () {
    render(new Date());
  });
})();
