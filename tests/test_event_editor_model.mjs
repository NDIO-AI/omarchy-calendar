// SPDX-License-Identifier: GPL-3.0-or-later
import assert from "node:assert/strict"
import { createRequire } from "node:module"
import test from "node:test"

process.env.TZ = "America/Chicago"
const require = createRequire(import.meta.url)
const editor = require("../EventEditorModel.js")

const calendars = [
  { key: "read", provider: "google", name: "Shared", writable: true, owned: false },
  { key: "google", provider: "google", name: "Work", writable: true, owned: true },
  { key: "outlook", provider: "microsoft", name: "Personal", writable: true, owned: true },
]

const event = {
  uid: "google:a:c:e", calendar_key: "google", title: "Design review",
  start: "2026-09-02T10:00:00-05:00", end: "2026-09-02T11:00:00-05:00",
  all_day: false, location: "Room B", description: "Notes",
  meeting_url: "https://meet.google.com/abc-defg-hij", recurrence_id: "series-1",
}

test("only owned writable calendars are mutation destinations", () => {
  assert.deepEqual(editor.destinations(calendars).map(item => item.key), ["google", "outlook"])
})

test("new and duplicated events open local drafts without mutating source data", () => {
  const created = editor.newDraft("2026-09-03", "14:15", "15:00", calendars, () => 0.25)
  const duplicated = editor.eventDraft(event, true, () => 0.5)

  assert.equal(created.calendar_key, "google")
  assert.equal(created.day, "2026-09-03")
  assert.equal(created.start, "14:15")
  assert.equal(created.operation, "create")
  assert.match(created.request_id, /^[0-9a-f-]{36}$/)
  assert.equal(duplicated.operation, "copy")
  assert.equal(duplicated.source_uid, event.uid)
  assert.equal(duplicated.online_meeting, "preserve")
  assert.equal(duplicated.recurrence.frequency, "none")
  assert.equal(event.title, "Design review")
})

test("duplicating a read-only event selects the first owned writable calendar", () => {
  const source = { ...event, calendar_key: "read" }

  const duplicated = editor.eventDraft(source, true, () => 0.5, calendars)

  assert.equal(duplicated.calendar_key, "google")
  assert.equal(duplicated.operation, "copy")
})

test("changing calendar switches an edit into copy mode", () => {
  const draft = editor.eventDraft(event, false, () => 0.5)

  assert.equal(editor.mode(draft, event), "update")
  assert.equal(editor.mode({ ...draft, calendar_key: "outlook" }, event), "copy")
  assert.equal(editor.mode({ ...draft, operation: "copy" }, event), "copy")
})

test("an existing recurring series keeps its schedule unless the user changes it", () => {
  const draft = editor.eventDraft(event, false, () => 0.5)
  assert.deepEqual(draft.recurrence, { frequency: "preserve", weekdays: [], end: "never" })
})

test("drag and resize apply 15 minute local draft changes only", () => {
  const draft = editor.eventDraft(event, false, () => 0.5)
  const moved = editor.shift(draft, 1, 30, 0)
  const resized = editor.shift(draft, 0, 0, -45)

  assert.equal(moved.day, "2026-09-03")
  assert.equal(moved.start, "10:30")
  assert.equal(moved.end, "11:30")
  assert.equal(resized.start, "10:00")
  assert.equal(resized.end, "10:15")
  assert.equal(draft.start, "10:00")
})

test("all-day drafts move by day without inventing times", () => {
  const draft = editor.eventDraft({ ...event, all_day: true }, false, () => 0.5)
  const moved = editor.shift(draft, -1, 120, 30)

  assert.equal(moved.day, "2026-09-01")
  assert.equal(moved.start, "")
  assert.equal(moved.end, "")
})

test("leaving all-day mode restores both default times in one draft update", () => {
  const allDay = editor.eventDraft({ ...event, all_day: true }, false, () => 0.5)
  const timed = editor.toggleAllDay(allDay)

  assert.equal(timed.all_day, false)
  assert.equal(timed.start, "09:00")
  assert.equal(timed.end, "10:00")
  assert.equal(allDay.start, "")
})

test("meeting choices respect destination support and provider limits", () => {
  const draft = editor.eventDraft(event, false, () => 0.5)
  const supported = calendars.map(item => item.key === "google"
    ? { ...item, meeting_providers: ["googleMeet"] }
    : item)
  const outlookEvent = { ...event, provider: "microsoft", calendar_key: "outlook" }
  const outlookDraft = editor.eventDraft(outlookEvent, false, () => 0.5)

  assert.deepEqual(editor.meetingOptions(draft, event, supported), ["preserve", "new", "none"])
  assert.deepEqual(editor.meetingOptions(draft, event, calendars), ["preserve", "none"])
  assert.deepEqual(editor.meetingOptions({ ...draft, calendar_key: "read" }, null, supported), ["none"])
  assert.deepEqual(editor.meetingOptions(outlookDraft, outlookEvent, calendars), ["preserve"])
  assert.deepEqual(editor.meetingOptions({ ...outlookDraft, calendar_key: "google" }, outlookEvent, supported), ["preserve", "new", "none"])
})

test("keyboard field order skips controls that are not visible", () => {
  const single = editor.newDraft("2026-09-03", "09:00", "10:00", calendars, () => 0.25)
  const selectedDays = {
    ...single,
    recurrence: { frequency: "selected_weekdays", weekdays: ["MO"], end: "never" },
  }

  assert.deepEqual(editor.visibleControls(single, "create", false), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 14, 15])
  assert.deepEqual(editor.visibleControls({ ...single, all_day: true }, "create", false), [0, 1, 2, 5, 6, 7, 8, 11, 14, 15])
  assert.deepEqual(editor.visibleControls(selectedDays, "update", true), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15])
  assert.deepEqual(editor.visibleControls({ ...single, recurrence: { frequency: "preserve" } }, "update", true), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 12, 13, 14, 15])
})

test("selected weekdays toggle without mutating the current draft", () => {
  const draft = {
    ...editor.newDraft("2026-09-03", "09:00", "10:00", calendars, () => 0.25),
    recurrence: { frequency: "selected_weekdays", weekdays: ["MO"], end: "never" },
  }

  const added = editor.toggleWeekday(draft, "TU")
  const removed = editor.toggleWeekday(added, "MO")

  assert.deepEqual(added.recurrence.weekdays, ["MO", "TU"])
  assert.deepEqual(removed.recurrence.weekdays, ["TU"])
  assert.deepEqual(draft.recurrence.weekdays, ["MO"])
})
