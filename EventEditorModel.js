// SPDX-License-Identifier: GPL-3.0-or-later

function pad2(value) {
  return (Number(value) < 10 ? "0" : "") + Number(value)
}

function dayKey(value) {
  var date = value instanceof Date ? value : new Date(value)
  return date.getFullYear() + "-" + pad2(date.getMonth() + 1) + "-" + pad2(date.getDate())
}

function timeKey(value) {
  var date = value instanceof Date ? value : new Date(value)
  return pad2(date.getHours()) + ":" + pad2(date.getMinutes())
}

function addDays(value, amount) {
  var parts = String(value).split("-")
  var date = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]))
  date.setDate(date.getDate() + Number(amount || 0))
  return dayKey(date)
}

function destinations(calendars) {
  return (calendars || []).filter(function(calendar) {
    return calendar.writable === true && calendar.owned === true
  })
}

function calendarFor(draft, calendars) {
  return (calendars || []).find(function(calendar) {
    return String(calendar.key || "") === String((draft || {}).calendar_key || "")
  })
}

function meetingOptions(draft, event, calendars) {
  var calendar = calendarFor(draft, calendars)
  var copying = Boolean(event) && (
    draft.operation === "copy" || String(draft.calendar_key || "") !== String(event.calendar_key || "")
  )
  if (event && event.meeting_url && !copying && event.provider === "microsoft") return ["preserve"]
  var options = event && event.meeting_url ? ["preserve"] : []
  if (calendar && (calendar.meeting_providers || []).length) options.push("new")
  options.push("none")
  return options
}

function requestId(random) {
  var source = random || Math.random
  var hex = ""
  for (var i = 0; i < 32; i++) hex += Math.floor(source() * 16).toString(16)
  return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-4" + hex.slice(13, 16)
    + "-a" + hex.slice(17, 20) + "-" + hex.slice(20, 32)
}

function baseDraft(random) {
  return {
    request_id: requestId(random),
    recurrence: { frequency: "none", weekdays: [], end: "never" },
    online_meeting: "none",
    scope: "single",
    source_uid: "",
  }
}

function newDraft(day, start, end, calendars, random) {
  var available = destinations(calendars)
  var draft = baseDraft(random)
  draft.calendar_key = available.length ? String(available[0].key || "") : ""
  draft.title = ""
  draft.day = String(day || dayKey(new Date()))
  draft.start = String(start || "09:00")
  draft.end = String(end || "10:00")
  draft.all_day = false
  draft.location = ""
  draft.notes = ""
  draft.operation = "create"
  return draft
}

function eventDraft(event, duplicate, random, calendars) {
  var source = event || {}
  var draft = baseDraft(random)
  draft.calendar_key = String(source.calendar_key || "")
  draft.title = String(source.title || "")
  draft.day = dayKey(source.start || new Date())
  draft.all_day = source.all_day === true
  draft.start = draft.all_day ? "" : timeKey(source.start)
  draft.end = draft.all_day ? "" : timeKey(source.end)
  draft.location = String(source.location || "")
  draft.notes = String(source.description || "")
  draft.online_meeting = source.meeting_url ? "preserve" : "none"
  draft.recurrence = {
    frequency: source.recurrence_id && !duplicate ? "preserve" : "none",
    weekdays: [], end: "never",
  }
  draft.source_uid = String(source.uid || "")
  draft.scope = "single"
  draft.operation = duplicate ? "copy" : "update"
  if (duplicate) {
    var available = destinations(calendars)
    var currentAvailable = available.some(function(calendar) {
      return String(calendar.key || "") === draft.calendar_key
    })
    if (!currentAvailable && available.length) draft.calendar_key = String(available[0].key || "")
  }
  return draft
}

function mode(draft, source) {
  if (!draft || !draft.source_uid) return "create"
  if (draft.operation === "copy") return "copy"
  if (!source || String(draft.calendar_key || "") !== String(source.calendar_key || "")) return "copy"
  return "update"
}

function minutes(value) {
  var parts = String(value).split(":")
  return Number(parts[0]) * 60 + Number(parts[1])
}

function minuteKey(value) {
  var bounded = Math.max(0, Math.min(1439, Number(value)))
  return pad2(Math.floor(bounded / 60)) + ":" + pad2(bounded % 60)
}

function shift(draft, dayAmount, minuteAmount, durationAmount) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  next.day = addDays(draft.day, dayAmount)
  if (draft.all_day) return next
  var duration = Math.max(15, minutes(draft.end) - minutes(draft.start) + Number(durationAmount || 0))
  var start = minutes(draft.start) + Number(minuteAmount || 0)
  while (start < 0) { start += 1440; next.day = addDays(next.day, -1) }
  while (start >= 1440) { start -= 1440; next.day = addDays(next.day, 1) }
  var end = Math.min(1439, start + duration)
  if (end - start < 15) start = Math.max(0, end - 15)
  next.start = minuteKey(start)
  next.end = minuteKey(end)
  return next
}

function toggleAllDay(draft) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  next.all_day = !draft.all_day
  if (!next.all_day && !next.start) {
    next.start = "09:00"
    next.end = "10:00"
  }
  return next
}

function visibleControls(draft, mode, recurring) {
  var controls = [0, 1, 2]
  if (!draft.all_day) controls.push(3, 4)
  controls.push(5, 6, 7, 8)
  var frequency = String((draft.recurrence || {}).frequency || "none")
  if (frequency === "selected_weekdays") controls.push(9)
  if (frequency !== "none" && frequency !== "preserve") controls.push(10)
  controls.push(11)
  if (recurring) controls.push(12)
  if (mode === "update") controls.push(13)
  controls.push(14, 15)
  return controls
}

function toggleWeekday(draft, weekday) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  var recurrence = {}
  for (var recurrenceKey in (draft.recurrence || {})) recurrence[recurrenceKey] = draft.recurrence[recurrenceKey]
  recurrence.weekdays = (recurrence.weekdays || []).slice()
  var index = recurrence.weekdays.indexOf(weekday)
  if (index >= 0) recurrence.weekdays.splice(index, 1)
  else recurrence.weekdays.push(weekday)
  next.recurrence = recurrence
  return next
}

if (typeof module !== "undefined") module.exports = {
  destinations: destinations,
  newDraft: newDraft,
  eventDraft: eventDraft,
  mode: mode,
  shift: shift,
  toggleAllDay: toggleAllDay,
  visibleControls: visibleControls,
  toggleWeekday: toggleWeekday,
  meetingOptions: meetingOptions,
  requestId: requestId,
}
