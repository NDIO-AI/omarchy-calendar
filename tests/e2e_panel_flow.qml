// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import Quickshell
import "." as FlightDeck

ShellRoot {
    id: shell
    property int failures: 0
    property int phase: 0
    property int polls: 0
    property string createdUid: ""
    readonly property string helper: String(Quickshell.env("FLIGHT_DECK_E2E_HELPER") || "")

    QtObject {
        id: service
        property string helperPath: shell.helper
        property bool syncing: false
        property string lastError: ""
        property int syncIntervalMinutes: 5
        property int syncRequests: 0
        signal revisionChanged()
        function requestSync() { syncRequests += 1; return true }
    }
    Item { id: anchor; width: 24; height: 24 }
    FlightDeck.Panel {
        id: panel
        width: 1400
        height: 720
        anchorItem: anchor
        calendarService: service
        settings: ({ defaultView: "today" })
    }

    function check(condition, label) {
        if (condition) console.log("E2E PASS " + label)
        else { failures += 1; console.error("E2E FAIL " + label) }
    }
    function advance(next) { phase = next; polls = 0 }
    function eventByUid(uid) {
        for (var i = 0; i < panel.cachedEvents.length; i++)
            if (String(panel.cachedEvents[i].uid) === String(uid)) return panel.cachedEvents[i]
        return null
    }
    function named(item, name) {
        if (!item) return null
        if (String(item.objectName || "") === name) return item
        var children = item.children || []
        for (var i = 0; i < children.length; i++) {
            var match = named(children[i], name)
            if (match) return match
        }
        return null
    }
    function stop() {
        console.log("E2E RESULT failures=" + failures)
        Qt.quit()
    }

    Timer {
        interval: 25
        repeat: true
        running: true
        onTriggered: {
            polls += 1
            if (polls > 400) { shell.check(false, "phase " + phase + " completed before timeout"); shell.stop(); return }
            var settings = shell.named(panel, "settingsSurface")
            var editor = shell.named(panel, "eventEditor")
            var source = shell.eventByUid("google:google-read:event")
            if (phase === 0) {
                shell.check(shell.helper !== "", "deterministic helper path is configured")
                panel.loadView(); panel.loadSetupStatus(); shell.advance(1)
            } else if (phase === 1 && !panel.loading && panel.cachedEvents.length === 6 && panel.setupProviders[0].accounts === 3) {
                var loaded = JSON.stringify({ events: panel.cachedEvents, calendars: panel.calendars, providers: panel.providers, demo: false })
                panel.cachedEvents = [Object.assign({}, source, { uid: "stale:friday", start: "2026-09-04T09:00:00-05:00", end: "2026-09-04T10:00:00-05:00" })]
                panel.selectedDay = new Date(2026, 8, 3); panel.nowTime = new Date("2026-09-03T14:30:00-05:00"); panel.pendingNow = true
                panel.applyView(loaded)
                shell.check(panel.selectedUid === "microsoft:outlook-read:event", "Now selects from the newly loaded day instead of stale view data")
                panel.selectedDay = new Date(2026, 8, 3); panel.cursorDate = panel.selectedDay; panel.selectedUid = source.uid
                panel.beginEdit(source)
                shell.check(panel.showSettings && !panel.showEditor && panel.pendingWriteIntent.kind === "edit", "read-only edit routes to Settings")
                shell.check(settings.controlIndex === settings.accountActionIndex("google", "google-read", "enable"), "Settings focuses the exact account")
                panel.cancelSettings(); panel.beginCreate(panel.selectedDay, 9 * 60)
                shell.check(panel.showSettings && !panel.showEditor && panel.pendingWriteIntent.kind === "create", "read-only create routes to Settings")
                panel.cancelSettings(); panel.beginDuplicate(source)
                shell.check(panel.showSettings && !panel.showEditor && panel.pendingWriteIntent.kind === "duplicate", "read-only duplicate routes to Settings")
                panel.cancelSettings(); panel.beginEdit(Object.assign({}, source, { has_attendees: true }))
                shell.check(!panel.showEditor && !panel.showSettings && panel.actionNotice.indexOf("attendees") >= 0, "ineligible edit explains the block")
                panel.shiftDraft(source.uid, 0, 15, 0)
                shell.check(panel.showSettings && panel.pendingWriteIntent.kind === "move" && panel.pendingWriteIntent.minuteAmount === 15, "read-only quick move keeps its pending delta")
                settings.activateCurrent(); shell.advance(2)
            } else if (phase === 2 && panel.showEditor && !panel.accountBusy) {
                shell.check(!panel.showSettings && !panel.showSetup, "successful consent returns to the calendar editor")
                shell.check(panel.editorSource.uid === "google:google-read:event", "successful consent resumes the selected edit")
                shell.check(panel.editorDraft.start === "09:15", "resumed quick move applies after consent")
                panel.moveActiveDraft(0, 15, 0)
                shell.check(panel.editorDraft.start === "09:30", "repeated quick move stays in one local draft")
                panel.saveDraft(); shell.advance(3)
            } else if (phase === 3 && !panel.mutationBusy && !panel.showEditor) {
                source = shell.eventByUid("google:google-read:event")
                if (!source || source.start.indexOf("09:30") < 0) return
                shell.check(source.start.indexOf("09:30") >= 0, "Save visibly updates the deterministic cache")
                var denied = shell.eventByUid("google:google-denial:event")
                panel.beginEdit(denied); settings.activateCurrent(); shell.advance(4)
            } else if (phase === 4 && !panel.accountBusy && panel.accountError.indexOf("denied") >= 0) {
                shell.check(panel.showSettings && !panel.showEditor, "denial stays in Settings")
                shell.check(panel.pendingWriteIntent !== null, "denial preserves the pending intent")
                panel.cancelSettings()
                var wrong = shell.eventByUid("google:google-wrong:event")
                panel.beginEdit(wrong); settings.activateCurrent(); shell.advance(5)
            } else if (phase === 5 && !panel.accountBusy && panel.accountError.indexOf("different account") >= 0) {
                shell.check(panel.showSettings && !panel.showEditor, "wrong-account consent stays in Settings")
                panel.cancelSettings()
                var outlook = shell.eventByUid("microsoft:outlook-read:event")
                panel.beginEdit(outlook); settings.activateCurrent(); shell.advance(6)
            } else if (phase === 6 && panel.showEditor && !panel.accountBusy) {
                shell.check(panel.editorSource.account_id === "outlook-read", "Settings enables the exact Outlook account")
                panel.cancelDraft(); source = shell.eventByUid("google:google-read:event")
                panel.beginDuplicate(source)
                panel.updateEditorDraft(Object.assign({}, panel.editorDraft, { calendar_key: "outlook-main", title: "Transferred copy" }))
                panel.saveDraft(); shell.advance(7)
            } else if (phase === 7 && !panel.mutationBusy && panel.pendingCopiedOriginalUid !== "") {
                shell.check(panel.pendingCopiedOriginalDeleteAvailable, "verified transfer offers a separate source action")
                panel.keepBothCopies(); source = shell.eventByUid("google:google-read:event")
                panel.beginEdit(source)
                panel.updateEditorDraft(Object.assign({}, panel.editorDraft, { title: "E2E conflict" }))
                panel.saveDraft(); shell.advance(8)
            } else if (phase === 8 && !panel.mutationBusy && panel.mutationError.indexOf("changed remotely") >= 0) {
                shell.check(panel.showEditor && panel.editorDraft.title === "E2E conflict", "conflict preserves the local draft")
                shell.check(service.syncRequests > 0, "conflict requests a provider refresh")
                panel.cancelDraft(); source = shell.eventByUid("google:google-read:event")
                panel.providers = [{ provider: "google", account_id: "google-read", connected: true, stale: true }]
                panel.beginEdit(source); panel.saveDraft()
                shell.check(panel.showEditor && !panel.mutationBusy, "offline Save is disabled without losing the draft")
                panel.cancelDraft(); panel.loadView(); shell.advance(9)
            } else if (phase === 9 && !panel.loading) {
                panel.beginCreate(new Date(2026, 8, 3), 17 * 60)
                panel.updateEditorDraft(Object.assign({}, panel.editorDraft, { title: "Created through installed QML" }))
                panel.saveDraft(); shell.advance(10)
            } else if (phase === 10 && !panel.mutationBusy && !panel.showEditor) {
                shell.createdUid = panel.selectedUid
                if (!shell.eventByUid(shell.createdUid)) return
                shell.check(shell.eventByUid(shell.createdUid).title === "Created through installed QML", "create reaches the helper through stdin")
                panel.beginEdit(shell.eventByUid(shell.createdUid))
                editor.revealDeleteConfirmation(); editor.controlIndex = 14; editor.activateCurrent(); shell.advance(11)
            } else if (phase === 11 && !panel.mutationBusy && !panel.showEditor && shell.eventByUid(shell.createdUid) === null) {
                shell.check(shell.eventByUid(shell.createdUid) === null, "confirmed delete removes the deterministic event")
                var recurring = shell.eventByUid("google:google-read:recurring")
                panel.beginEdit(recurring)
                shell.check(editor.recurring, "recurring event exposes occurrence and series scope")
                editor.setScope("series")
                shell.check(panel.editorDraft.scope === "series", "entire-series scope remains explicit")
                panel.cancelDraft(); source = shell.eventByUid("google:google-read:event")
                panel.beginDuplicate(source)
                panel.updateEditorDraft(Object.assign({}, panel.editorDraft, { calendar_key: "outlook-main", title: "Move result" }))
                panel.saveDraft(); shell.advance(12)
            } else if (phase === 12 && !panel.mutationBusy && panel.pendingCopiedOriginalUid !== "") {
                panel.deleteCopiedOriginal(); shell.check(panel.confirmCopiedOriginalDelete, "Delete original requires confirmation")
                panel.deleteCopiedOriginal(); shell.advance(13)
            } else if (phase === 13 && !panel.mutationBusy && panel.pendingCopiedOriginalUid === "" && shell.eventByUid("google:google-read:event") === null) {
                shell.check(shell.eventByUid("google:google-read:event") === null, "confirmed source deletion runs only after copy verification")
                settings.focusAccountAction("google", "google-wrong", "disconnect")
                panel.showSettings = true; settings.activateCurrent(); settings.activateCurrent(); shell.advance(14)
            } else if (phase === 14 && !panel.accountBusy && shell.eventByUid("google:google-wrong:event") === null) {
                shell.check(shell.eventByUid("google:google-wrong:event") === null, "disconnect targets only the focused account")
                shell.stop()
            }
        }
    }
}
