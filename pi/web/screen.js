// Decides whether the dashboard screen should be asleep.
// Pure functions with no DOM access, so tests/screen.test.js can run them in Node.
(function (root) {
  // "HH:MM" -> minutes since midnight, or null when it is empty or malformed
  function minutesOf(hhmm) {
    const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm || "");
    if (!m || +m[1] > 23 || +m[2] > 59) return null;
    return +m[1] * 60 + +m[2];
  }

  // Is `date` inside the daily off window? Handles windows that cross midnight (23:00 to 07:00).
  function inOffWindow(cfg, date) {
    const off = minutesOf(cfg.off_at), on = minutesOf(cfg.on_at);
    if (off === null || on === null || off === on) return false;
    const now = date.getHours() * 60 + date.getMinutes();
    return off < on ? now >= off && now < on : now >= off || now < on;
  }

  // cfg is the "screen" block of config.json. Times are in milliseconds.
  //  - In the off window the screen sleeps unless it was touched (or an alert arrived) recently.
  //  - Outside it, the screen sleeps only after idle_minutes without a touch (0 = never).
  function shouldSleep(cfg, nowMs, lastTouchMs, alertUntilMs) {
    if (nowMs < alertUntilMs) return false;
    const idle = nowMs - lastTouchMs;
    if (inOffWindow(cfg, new Date(nowMs))) return idle > (cfg.wake_seconds || 60) * 1000;
    return cfg.idle_minutes > 0 && idle > cfg.idle_minutes * 60000;
  }

  const api = { minutesOf, inOffWindow, shouldSleep };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HomelabScreen = api;
})(this);
