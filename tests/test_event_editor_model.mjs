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
  { key: "hidden", provider: "google", name: "Hidden", writable: true, owned: true, sync_enabled: false },
]

const event = {
  uid: "google:a:c:e", provider: "google", account_id: "google-account",
  calendar_key: "google", title: "Design review", organizer_owned: true,
  start: "2026-09-02T10:00:00-05:00", end: "2026-09-02T11:00:00-05:00",
  all_day: false, location: "Room B", description: "Notes",
  meeting_url: "https://meet.google.com/abc-defg-hij", recurrence_id: "series-1",
  revision: '"event-etag"', series_revision: '"series-etag"',
  series_start: "2026-08-05T10:00:00-05:00", series_end: "2026-08-05T11:00:00-05:00",
}

const providerState = [
  { provider: "google", editing_account_ids: [] },
  { provider: "microsoft", editing_account_ids: ["microsoft-account"] },
]

const accountCalendars = calendars.map(item => ({
  ...item,
  account_id: item.provider === "google" ? "google-account" : "microsoft-account",
}))

test("only enabled, owned, writable calendars are mutation destinations", () => {
  assert.deepEqual(editor.destinations(calendars).map(item => item.key), ["google", "outlook"])
})

test("only calendars whose exact account granted editing reach the editor", () => {
  assert.deepEqual(
    editor.editableDestinations(accountCalendars, providerState).map(item => item.key),
    ["outlook"],
  )
})

test("write intents share one permission and eligibility decision", () => {
  assert.deepEqual(editor.writeDecision("edit", event, accountCalendars, providerState), {
    action: "settings", provider: "google", account_id: "google-account", reason: "",
  })
  assert.deepEqual(editor.writeDecision("create", null, accountCalendars, providerState), {
    action: "editor", provider: "microsoft", account_id: "microsoft-account", reason: "",
  })
  assert.equal(
    editor.writeDecision("edit", { ...event, has_attendees: true }, accountCalendars, providerState).reason,
    "Events with attendees cannot be edited. Duplicate the event instead.",
  )
  assert.equal(
    editor.writeDecision("edit", { ...event, organizer_owned: false }, accountCalendars, providerState).action,
    "blocked",
  )
  assert.equal(
    editor.writeDecision("duplicate", event, accountCalendars, [
      { provider: "google", editing_account_ids: [] },
      { provider: "microsoft", editing_account_ids: [] },
    ]).action,
    "settings",
  )
})

test("edit action copy reflects authorization without opening a false editor", () => {
  assert.equal(editor.editAction(event, accountCalendars, providerState), "Enable editing")
  assert.equal(editor.editAction(
    { ...event, account_id: "microsoft-account", provider: "microsoft", calendar_key: "outlook" },
    accountCalendars,
    providerState,
  ), "Edit")
  assert.equal(editor.editAction({ ...event, has_attendees: true }, accountCalendars, providerState), "Cannot edit")
})

test("new and duplicated events open local drafts without mutating source data", () => {
  const created = editor.newDraft("2026-09-03", "14:15", "15:00", calendars, () => 0.25)
  const duplicated = editor.eventDraft(event, true, () => 0.5)

  assert.equal(created.calendar_key, "google")
  assert.equal(created.day, "2026-09-03")
  assert.equal(created.start, "14:15")
  assert.equal(created.end_day, "2026-09-03")
  assert.equal(created.operation, "create")
  assert.match(created.request_id, /^[0-9a-f-]{36}$/)
  assert.equal(duplicated.operation, "copy")
  assert.equal(duplicated.source_uid, event.uid)
  assert.equal(duplicated.source_revision, event.revision)
  assert.equal(duplicated.series_revision, event.series_revision)
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

test("changing a recurring destination preserves scope and normalizes its meeting choice", () => {
  const draft = {
    ...editor.eventDraft(event, false, () => 0.5),
    scope: "series",
    online_meeting: "new",
  }

  const changed = editor.withCalendar(draft, "outlook", event, calendars)

  assert.equal(changed.calendar_key, "outlook")
  assert.equal(changed.scope, "series")
  assert.equal(changed.recurrence.frequency, "preserve")
  assert.equal(changed.online_meeting, "preserve")
  assert.equal(draft.calendar_key, "google")
})

test("an existing recurring series keeps its schedule unless the user changes it", () => {
  const draft = editor.eventDraft(event, false, () => 0.5)
  assert.deepEqual(draft.recurrence, { frequency: "preserve", weekdays: [], end: "never" })
  assert.equal(draft.series_start, event.series_start)
  assert.equal(draft.series_end, event.series_end)
})

test("a recurring master keeps its schedule without an occurrence recurrence ID", () => {
  const master = {
    ...event,
    recurrence_id: "",
    event_type: "series",
    recurrence: ["RRULE:FREQ=WEEKLY"],
  }

  const draft = editor.eventDraft(master, false, () => 0.5)
  const duplicate = editor.eventDraft(master, true, () => 0.5, calendars)

  assert.deepEqual(draft.recurrence, { frequency: "preserve", weekdays: [], end: "never" })
  assert.equal(draft.series_only, true)
  assert.equal(draft.scope, "series")
  assert.equal(editor.visibleControls(draft, "update", true, false, true).includes(13), false)
  assert.equal(duplicate.series_only, false)
  assert.equal(duplicate.scope, "single")
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
  const draft = editor.eventDraft({
    ...event, all_day: true,
    start: "2026-09-02T00:00:00-05:00", end: "2026-09-05T00:00:00-05:00",
  }, false, () => 0.5)
  const moved = editor.shift(draft, -1, 120, 30)

  assert.equal(moved.day, "2026-09-01")
  assert.equal(moved.end_day, "2026-09-04")
  assert.equal(moved.start, "")
  assert.equal(moved.end, "")
})

test("quick movement repeats on one local draft without mutating the event", () => {
  const draft = editor.eventDraft(event, false, () => 0.5, accountCalendars)
  const moved = editor.shift(editor.shift(editor.shift(draft, 0, 15, 0), 0, 15, 0), 1, 0, 0)

  assert.deepEqual([moved.day, moved.start, moved.end], ["2026-09-03", "10:30", "11:30"])
  assert.deepEqual([event.start, event.end], ["2026-09-02T10:00:00-05:00", "2026-09-02T11:00:00-05:00"])
})

test("all-day drafts use provider date semantics across workstation timezones", () => {
  const draft = editor.eventDraft({
    ...event, all_day: true, start_day: "2026-09-02", end_day: "2026-09-05",
    start: "2026-09-02T00:00:00+09:00", end: "2026-09-05T00:00:00+09:00",
  }, false, () => 0.5)

  assert.deepEqual([draft.day, draft.end_day], ["2026-09-02", "2026-09-05"])
})

test("timed drafts derive dates and times in the same workstation timezone", () => {
  const draft = editor.eventDraft({
    ...event, start_day: "2026-09-02", end_day: "2026-09-02",
    start: "2026-09-02T00:30:00+09:00", end: "2026-09-02T01:30:00+09:00",
  }, false, () => 0.5)

  assert.deepEqual([draft.day, draft.start, draft.end_day, draft.end], [
    "2026-09-01", "10:30", "2026-09-01", "11:30",
  ])
})

test("leaving all-day mode restores both default times in one draft update", () => {
  const allDay = editor.eventDraft({
    ...event, all_day: true,
    start: "2026-09-02T00:00:00-05:00", end: "2026-09-03T00:00:00-05:00",
  }, false, () => 0.5)
  const timed = editor.toggleAllDay(allDay)

  assert.equal(timed.all_day, false)
  assert.equal(timed.start, "09:00")
  assert.equal(timed.end, "10:00")
  assert.equal(timed.end_day, timed.day)
  assert.equal(allDay.start, "")
})

test("overnight drafts preserve their end day while moving and editing times", () => {
  const overnight = editor.eventDraft({
    ...event,
    start: "2026-09-02T23:30:00-05:00", end: "2026-09-03T01:00:00-05:00",
  }, false, () => 0.5)

  const moved = editor.shift(overnight, 1, 60, 30)
  const edited = editor.withTime(editor.newDraft("2026-09-03", "22:00", "23:00", calendars), "end", "01:00")
  const changedDay = editor.withDay(overnight, "2026-09-08")
  const completedDay = editor.withDay(editor.withDay(overnight, "2026-09-0"), "2026-09-08")

  assert.deepEqual([moved.day, moved.start, moved.end_day, moved.end], ["2026-09-04", "00:30", "2026-09-04", "02:30"])
  assert.equal(edited.end_day, "2026-09-04")
  assert.deepEqual([changedDay.day, changedDay.end_day], ["2026-09-08", "2026-09-09"])
  assert.deepEqual([completedDay.day, completedDay.end_day], ["2026-09-08", "2026-09-09"])
})

test("changing the start time preserves duration instead of creating a day-long event", () => {
  const draft = editor.newDraft("2026-09-03", "10:45", "11:00", calendars, () => 0.25)

  const changed = editor.withTime(draft, "start", "11:00")

  assert.deepEqual([changed.day, changed.start, changed.end_day, changed.end], [
    "2026-09-03", "11:00", "2026-09-03", "11:15",
  ])
})

test("a late empty slot creates a one-hour event ending the next day", () => {
  const draft = editor.newDraft("2026-09-03", "23:30", "00:30", calendars, () => 0.25)

  assert.deepEqual([draft.day, draft.start, draft.end_day, draft.end], [
    "2026-09-03", "23:30", "2026-09-04", "00:30",
  ])
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

  assert.deepEqual(editor.visibleControls(single, "create", false, false), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 15, 16])
  assert.deepEqual(editor.visibleControls({ ...single, all_day: true }, "create", false, false), [0, 1, 2, 5, 6, 7, 8, 11, 15, 16])
  assert.deepEqual(editor.visibleControls(selectedDays, "update", true, true), [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16])
  assert.deepEqual(editor.visibleControls({ ...single, recurrence: { frequency: "preserve" } }, "update", true, false), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13, 14, 15, 16])
  assert.deepEqual(editor.visibleControls(single, "update", false, false, false), [0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 15, 16])
})

test("only remote-change conflicts require an immediate cache refresh", () => {
  assert.equal(editor.isMutationConflict("Event changed remotely. Draft preserved; review the refreshed event."), true)
  assert.equal(editor.isMutationConflict("Event changed remotely. Delete cancelled; review the refreshed event."), true)
  assert.equal(editor.isMutationConflict("Event title is required"), false)
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

test("recurrence ending choices initialize the values shown in the editor", () => {
  const draft = {
    ...editor.newDraft("2026-09-03", "09:00", "10:00", calendars, () => 0.25),
    recurrence: { frequency: "weekly", weekdays: ["TH"], end: "never", count: 0, until: "" },
  }

  const counted = editor.withRecurrenceEnding(draft, "count")
  const dated = editor.withRecurrenceEnding(draft, "date")
  const never = editor.withRecurrenceEnding({ ...draft, recurrence: counted.recurrence }, "never")

  assert.deepEqual(counted.recurrence, {
    frequency: "weekly", weekdays: ["TH"], end: "count", count: 1, until: "",
  })
  assert.deepEqual(dated.recurrence, {
    frequency: "weekly", weekdays: ["TH"], end: "date", count: 0, until: "2026-09-03",
  })
  assert.deepEqual(never.recurrence, {
    frequency: "weekly", weekdays: ["TH"], end: "never", count: 0, until: "",
  })
  assert.equal(draft.recurrence.end, "never")
})
