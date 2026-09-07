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

function dayDistance(start, end) {
  var first = String(start).split("-").map(Number)
  var last = String(end || start).split("-").map(Number)
  return Math.round((Date.UTC(last[0], last[1] - 1, last[2]) - Date.UTC(first[0], first[1] - 1, first[2])) / 86400000)
}

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return false
  var parts = String(value).split("-").map(Number)
  var date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]))
  return date.getUTCFullYear() === parts[0] && date.getUTCMonth() === parts[1] - 1 && date.getUTCDate() === parts[2]
}

function validTime(value) {
  if (!/^\d{2}:\d{2}$/.test(String(value))) return false
  var parts = String(value).split(":").map(Number)
  return parts[0] >= 0 && parts[0] < 24 && parts[1] >= 0 && parts[1] < 60
}

function destinations(calendars) {
  return (calendars || []).filter(function(calendar) {
    return calendar.writable === true && calendar.owned === true && calendar.sync_enabled !== false
  })
}

function accountCanEdit(calendar, providers) {
  if (!calendar) return false
  var state = (providers || []).find(function(provider) {
    return String(provider.provider || "") === String(calendar.provider || "")
  })
  return Boolean(state) && (state.editing_account_ids || []).indexOf(String(calendar.account_id || "")) >= 0
}

function editableDestinations(calendars, providers) {
  return destinations(calendars).filter(function(calendar) {
    return accountCanEdit(calendar, providers)
  })
}

function sourceCalendar(event, calendars) {
  return (calendars || []).find(function(calendar) {
    return String(calendar.key || "") === String((event || {}).calendar_key || "")
  })
}

function editRestriction(event, calendars) {
  if (!event) return "No event is selected."
  if (event.has_attendees === true)
    return "Events with attendees cannot be edited. Duplicate the event instead."
  if (event.organizer_owned !== true)
    return "Only events you organize can be edited. Duplicate the event instead."
  var calendar = sourceCalendar(event, calendars)
  if (!calendar || calendar.writable !== true || calendar.owned !== true || calendar.sync_enabled === false)
    return "This calendar cannot be edited. Duplicate the event instead."
  return ""
}

function writeDecision(intent, event, calendars, providers) {
  var editable = editableDestinations(calendars, providers)
  if (intent === "edit" || intent === "move") {
    var restriction = editRestriction(event, calendars)
    if (restriction) return { action: "blocked", provider: "", account_id: "", reason: restriction }
    var source = sourceCalendar(event, calendars)
    return accountCanEdit(source, providers)
      ? { action: "editor", provider: source.provider, account_id: source.account_id, reason: "" }
      : { action: "settings", provider: source.provider, account_id: source.account_id, reason: "" }
  }
  if (editable.length)
    return { action: "editor", provider: editable[0].provider, account_id: editable[0].account_id, reason: "" }
  var available = destinations(calendars)
  var preferred = intent === "duplicate" ? sourceCalendar(event, available) : null
  var target = preferred || available[0]
  return target
    ? { action: "settings", provider: target.provider, account_id: target.account_id, reason: "" }
    : { action: "blocked", provider: "", account_id: "", reason: "No owned calendar is available for editing." }
}

function editAction(event, calendars, providers) {
  var decision = writeDecision("edit", event, calendars, providers)
  return decision.action === "editor" ? "Edit" : decision.action === "settings" ? "Enable editing" : "Cannot edit"
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

function withCalendar(draft, calendarKey, event, calendars) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  next.calendar_key = String(calendarKey || "")
  var options = meetingOptions(next, event, calendars)
  if (options.indexOf(String(next.online_meeting || "none")) < 0)
    next.online_meeting = options.indexOf("preserve") >= 0 ? "preserve" : "none"
  return next
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
    source_revision: "",
    series_revision: "",
    series_start: "",
    series_end: "",
    series_only: false,
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
  draft.end_day = validTime(draft.start) && validTime(draft.end) && minutes(draft.end) <= minutes(draft.start)
    ? addDays(draft.day, 1) : draft.day
  draft.day_span = dayDistance(draft.day, draft.end_day)
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
  draft.all_day = source.all_day === true
  draft.day = draft.all_day
    ? String(source.start_day || dayKey(source.start || new Date()))
    : dayKey(source.start || new Date())
  draft.end_day = draft.all_day
    ? String(source.end_day || dayKey(source.end || source.start || new Date()))
    : dayKey(source.end || source.start || new Date())
  draft.day_span = dayDistance(draft.day, draft.end_day)
  draft.start = draft.all_day ? "" : timeKey(source.start)
  draft.end = draft.all_day ? "" : timeKey(source.end)
  draft.location = String(source.location || "")
  draft.notes = String(source.description || "")
  draft.online_meeting = source.meeting_url ? "preserve" : "none"
  var recurring = Boolean(source.recurrence_id || source.event_type === "series" || (source.recurrence || []).length)
  draft.recurrence = {
    frequency: recurring && !duplicate ? "preserve" : "none",
    weekdays: [], end: "never",
  }
  draft.source_uid = String(source.uid || "")
  draft.source_revision = String(source.revision || "")
  draft.series_revision = String(source.series_revision || "")
  draft.series_start = String(source.series_start || "")
  draft.series_end = String(source.series_end || "")
  draft.series_only = !duplicate && source.event_type === "series" && !source.recurrence_id
  draft.scope = draft.series_only ? "series" : "single"
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
  next.end_day = addDays(draft.end_day || draft.day, dayAmount)
  if (draft.all_day) return next
  var duration = Math.max(15,
    dayDistance(draft.day, draft.end_day || draft.day) * 1440
      + minutes(draft.end) - minutes(draft.start) + Number(durationAmount || 0))
  var start = minutes(draft.start) + Number(minuteAmount || 0)
  while (start < 0) { start += 1440; next.day = addDays(next.day, -1) }
  while (start >= 1440) { start -= 1440; next.day = addDays(next.day, 1) }
  var endTotal = start + duration
  var end = endTotal % 1440
  next.start = minuteKey(start)
  next.end = minuteKey(end)
  next.end_day = addDays(next.day, Math.floor(endTotal / 1440))
  next.day_span = dayDistance(next.day, next.end_day)
  return next
}

function toggleAllDay(draft) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  next.all_day = !draft.all_day
  var span = Math.max(0, dayDistance(draft.day, draft.end_day || draft.day))
  if (next.all_day) {
    next.start = ""
    next.end = ""
    next.end_day = addDays(next.day, Math.max(1, span))
  } else if (!next.start) {
    next.start = "09:00"
    next.end = "10:00"
    next.end_day = addDays(next.day, Math.max(0, span - 1))
  }
  next.day_span = dayDistance(next.day, next.end_day)
  return next
}

function withDay(draft, value) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  var span = Number.isFinite(Number(draft.day_span))
    ? Number(draft.day_span) : dayDistance(draft.day, draft.end_day || draft.day)
  next.day = String(value)
  if (validDay(next.day)) next.end_day = addDays(next.day, span)
  next.day_span = span
  return next
}

function withTime(draft, key, value) {
  var next = {}
  for (var name in draft) next[name] = draft[name]
  next[key] = String(value)
  if (key === "start" && validTime(next.start) && validTime(draft.start) && validTime(draft.end)) {
    var duration = Math.max(15,
      dayDistance(draft.day, draft.end_day || draft.day) * 1440
        + minutes(draft.end) - minutes(draft.start))
    var endTotal = minutes(next.start) + duration
    next.end = minuteKey(endTotal % 1440)
    next.end_day = addDays(next.day, Math.floor(endTotal / 1440))
    next.day_span = dayDistance(next.day, next.end_day)
    return next
  }
  var span = dayDistance(next.day, next.end_day || next.day)
  if (span <= 1 && validTime(next.start) && validTime(next.end))
    next.end_day = addDays(next.day, minutes(next.end) <= minutes(next.start) ? 1 : 0)
  next.day_span = dayDistance(next.day, next.end_day)
  return next
}

function visibleControls(draft, mode, recurring, hasMeeting, canDelete) {
  var controls = [0, 1, 2]
  if (!draft.all_day) controls.push(3, 4)
  controls.push(5, 6, 7, 8)
  var frequency = String((draft.recurrence || {}).frequency || "none")
  if (frequency === "selected_weekdays") controls.push(9)
  if (frequency !== "none" && frequency !== "preserve") controls.push(10)
  controls.push(11)
  if (hasMeeting) controls.push(12)
  if (recurring && draft.series_only !== true) controls.push(13)
  if (mode === "update" && canDelete !== false) controls.push(14)
  controls.push(15, 16)
  return controls
}

function isMutationConflict(error) {
  return String(error || "").indexOf("changed remotely") >= 0
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

function withRecurrenceEnding(draft, ending) {
  var next = {}
  for (var key in draft) next[key] = draft[key]
  var recurrence = {}
  for (var recurrenceKey in (draft.recurrence || {})) recurrence[recurrenceKey] = draft.recurrence[recurrenceKey]
  recurrence.end = String(ending)
  recurrence.count = recurrence.end === "count" ? Math.max(1, Number(recurrence.count || 0)) : 0
  recurrence.until = recurrence.end === "date" && validDay(recurrence.until)
    ? recurrence.until : recurrence.end === "date" ? String(draft.day || "") : ""
  next.recurrence = recurrence
  return next
}

if (typeof module !== "undefined") module.exports = {
  destinations: destinations,
  editableDestinations: editableDestinations,
  writeDecision: writeDecision,
  editAction: editAction,
  newDraft: newDraft,
  eventDraft: eventDraft,
  mode: mode,
  shift: shift,
  toggleAllDay: toggleAllDay,
  withDay: withDay,
  withTime: withTime,
  visibleControls: visibleControls,
  isMutationConflict: isMutationConflict,
  toggleWeekday: toggleWeekday,
  withRecurrenceEnding: withRecurrenceEnding,
  withCalendar: withCalendar,
  meetingOptions: meetingOptions,
  requestId: requestId,
}
