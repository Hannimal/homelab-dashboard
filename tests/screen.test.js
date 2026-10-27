const test = require("node:test");
const assert = require("node:assert/strict");
const { minutesOf, inOffWindow, shouldSleep } = require("../pi/web/screen.js");

const at = (h, m = 0) => new Date(2026, 9, 27, h, m).getTime();
const night = { off_at: "23:00", on_at: "07:00", wake_seconds: 60, idle_minutes: 0 };

test("minutesOf parses HH:MM and rejects junk", () => {
  assert.equal(minutesOf("07:30"), 450);
  assert.equal(minutesOf("7:05"), 425);
  assert.equal(minutesOf("00:00"), 0);
  for (const bad of ["", null, undefined, "24:00", "12:60", "noon", "1230"]) assert.equal(minutesOf(bad), null, String(bad));
});

test("a window that crosses midnight covers late evening and early morning", () => {
  const d = (h, m) => new Date(at(h, m));
  assert.equal(inOffWindow(night, d(22, 59)), false);
  assert.equal(inOffWindow(night, d(23, 0)), true);
  assert.equal(inOffWindow(night, d(3, 30)), true);
  assert.equal(inOffWindow(night, d(6, 59)), true);
  assert.equal(inOffWindow(night, d(7, 0)), false);
  assert.equal(inOffWindow(night, d(12, 0)), false);
});

test("a same-day window works too", () => {
  const lunch = { off_at: "12:00", on_at: "13:30" };
  assert.equal(inOffWindow(lunch, new Date(at(12, 0))), true);
  assert.equal(inOffWindow(lunch, new Date(at(13, 29))), true);
  assert.equal(inOffWindow(lunch, new Date(at(13, 30))), false);
});

test("missing, half-set or equal times mean no window", () => {
  const d = new Date(at(3, 0));
  assert.equal(inOffWindow({}, d), false);
  assert.equal(inOffWindow({ off_at: "23:00" }, d), false);
  assert.equal(inOffWindow({ off_at: "23:00", on_at: "23:00" }, d), false);
  assert.equal(inOffWindow({ off_at: "bad", on_at: "07:00" }, d), false);
});

test("nothing configured: never sleeps", () => {
  assert.equal(shouldSleep({}, at(3), at(0), 0), false);
  assert.equal(shouldSleep({ idle_minutes: 0 }, at(3), at(0), 0), false);
});

test("idle timeout sleeps only after the idle time has passed", () => {
  const cfg = { idle_minutes: 5 };
  const t = at(14);
  assert.equal(shouldSleep(cfg, t, t - 4 * 60000, 0), false);
  assert.equal(shouldSleep(cfg, t, t - 5 * 60000 - 1, 0), true);
});

test("inside the off window the screen sleeps unless touched recently", () => {
  const t = at(2);
  assert.equal(shouldSleep(night, t, t - 3600 * 1000, 0), true);
  assert.equal(shouldSleep(night, t, t - 30 * 1000, 0), false);   // woken by a touch 30 s ago
  assert.equal(shouldSleep(night, t, t - 61 * 1000, 0), true);    // wake_seconds (60) has run out
});

test("wake_seconds defaults to 60 and can be changed", () => {
  const t = at(2);
  const cfg = { off_at: "23:00", on_at: "07:00" };
  assert.equal(shouldSleep(cfg, t, t - 59 * 1000, 0), false);
  assert.equal(shouldSleep(cfg, t, t - 61 * 1000, 0), true);
  assert.equal(shouldSleep({ ...cfg, wake_seconds: 300 }, t, t - 200 * 1000, 0), false);
});

test("an alert keeps the screen awake until its wake time ends", () => {
  const t = at(2);
  assert.equal(shouldSleep(night, t, t - 3600 * 1000, t + 10 * 1000), false);
  assert.equal(shouldSleep(night, t, t - 3600 * 1000, t - 1), true);
});

test("the idle timeout does not apply inside the window, the window rule does", () => {
  const cfg = { ...night, idle_minutes: 60 };
  const t = at(2);
  assert.equal(shouldSleep(cfg, t, t - 2 * 60 * 1000, 0), true);  // 2 min idle, window says sleep
  assert.equal(shouldSleep(cfg, at(14), at(14) - 2 * 60 * 1000, 0), false);
});
