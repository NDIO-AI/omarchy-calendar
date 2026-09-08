// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtTest
import "../.." as FlightDeck

TestCase {
    id: testCase
    name: "EventEditorStatus"
    width: 352
    height: 520
    visible: true
    when: windowShown

    readonly property var colors: ({
        background: "#16161e",
        surface: "#1f2335",
        foreground: "#c0caf5",
        muted: "#9aa5ce",
        accent: "#7aa2f7",
        border: "#3b4261",
        positive: "#9ece6a",
        urgent: "#f7768e"
    })
    readonly property var writableCalendars: [{
        key: "google-primary",
        provider: "google",
        account_id: "test-account",
        account_label: "owner@example.com",
        name: "Personal",
        writable: true,
        owned: true,
        sync_enabled: true,
        meeting_providers: ["googleMeet"]
    }]
    readonly property var ownedEvent: ({
        uid: "google:test:personal:event",
        calendar_key: "google-primary",
        organizer_owned: true,
        has_attendees: false,
        recurrence_id: "",
        recurrence: [],
        event_type: "default"
    })
    property var draft: ({
        operation: "update",
        source_uid: ownedEvent.uid,
        calendar_key: "google-primary",
        title: "Calendar design review",
        day: "2026-09-02",
        end_day: "2026-09-02",
        day_span: 0,
        start: "10:00",
        end: "11:00",
        all_day: false,
        location: "Conference Room B",
        notes: "Review the calendar editor status and scrolling behavior.",
        recurrence: { frequency: "none", weekdays: [], end: "never" },
        online_meeting: "none",
        scope: "single",
        series_only: false
    })

    FlightDeck.EventEditor {
        id: editor
        anchors.fill: parent
        draft: testCase.draft
        eventData: testCase.ownedEvent
        calendars: testCase.writableCalendars
        mode: "update"
        palette: testCase.colors
        fontFamily: "monospace"
        textScale: 1
    }

    SignalSpy {
        id: duplicateSpy
        target: editor
        signalName: "duplicateRequested"
    }

    SignalSpy {
        id: submitSpy
        target: editor
        signalName: "saveRequested"
    }

    function resetStatus() {
        editor.offline = false;
        editor.errorText = "";
        editor.mode = "update";
        editor.eventData = ownedEvent;
        editor.calendars = writableCalendars;
        editor.confirmDelete = false;
        wait(0);
        editor.resetInteraction();
        wait(0);
    }

    function findText(item, value) {
        if (item.text !== undefined && String(item.text) === value)
            return item;
        var children = item.children || [];
        for (var index = 0; index < children.length; index++) {
            var match = findText(children[index], value);
            if (match !== null)
                return match;
        }
        return null;
    }

    function nextSibling(item) {
        var siblings = item.parent.children;
        for (var index = 0; index < siblings.length - 1; index++) {
            if (siblings[index] === item)
                return siblings[index + 1];
        }
        return null;
    }

    function editorY(item) {
        return item.mapToItem(editor, 0, 0).y;
    }

    function test_compact_vertical_rhythm() {
        resetStatus();

        var title = findText(editor, "Title");
        var calendar = findText(editor, "Calendar");
        var location = findText(editor, "Location");
        var notes = findText(editor, "Notes");
        var recurrence = findText(editor, "Recurrence");
        var editHeading = findText(editor, "EDIT EVENT");
        var cancel = findText(editor, "Cancel  Esc");
        var hints = findChild(editor, "editorKeyboardHints");
        var scroll = findChild(editor, "editorScroll");

        verify(title !== null);
        verify(calendar !== null);
        verify(location !== null);
        verify(notes !== null);
        verify(recurrence !== null);
        verify(editHeading !== null);
        verify(cancel !== null);
        verify(hints !== null);
        verify(scroll !== null);

        var titleControl = nextSibling(title);
        var calendarControl = nextSibling(calendar);
        var locationControl = nextSibling(location);
        var notesControl = nextSibling(notes);
        var recurrenceControl = nextSibling(recurrence);
        var footer = cancel.parent.parent;

        compare(titleControl.y - title.y - title.height, 6);
        compare(calendar.y + calendar.topPadding - titleControl.y - titleControl.height, 14);
        compare(location.y + location.topPadding - findText(editor, "All-day").parent.y - findText(editor, "All-day").parent.height, 14);
        compare(recurrence.y + recurrence.topPadding - notesControl.y - notesControl.height, 14);

        compare(titleControl.height, 38);
        compare(calendarControl.height, 38);
        compare(locationControl.height, 38);
        compare(recurrenceControl.height, 38);
        compare(notesControl.height, 80);
        compare(cancel.parent.height, 38);

        compare(editorY(editHeading), 14);
        compare(scroll.y - editorY(editHeading) - editHeading.height, 14);
        compare(footer.y - scroll.y - scroll.height, 14);
        compare(editor.height - footer.y - footer.height, 14);
        compare(scroll.contentHeight - hints.y - hints.height, 14);
    }

    function test_permission_gate_is_not_rendered_as_an_editable_form() {
        verify(!("needsPermission" in editor));
    }

    function test_scrollbar_never_overlaps_editor_controls() {
        resetStatus();

        var title = findText(editor, "Title");
        var titleControl = nextSibling(title);
        var endPlus = findChild(editor, "endPlus");
        var scrollbar = findChild(editor, "editorScrollAffordance");

        verify(titleControl !== null);
        verify(endPlus !== null);
        verify(scrollbar !== null);

        var formRight = titleControl.parent.mapToItem(editor, titleControl.parent.width, 0).x;
        var endPlusRight = endPlus.mapToItem(editor, endPlus.width, 0).x;
        var scrollbarLeft = scrollbar.mapToItem(editor, 0, 0).x;

        verify(formRight < scrollbarLeft);
        verify(endPlusRight < scrollbarLeft);
    }

    function test_ctrl_enter_submits_from_a_focused_input() {
        resetStatus();
        submitSpy.clear();
        var titleInput = nextSibling(findText(editor, "Title")).children[0];
        titleInput.forceActiveFocus();

        keyClick(Qt.Key_Return, Qt.ControlModifier);

        compare(submitSpy.count, 1);
    }

    function test_status_transition_reveals_callout_data() {
        return [
            { tag: "offline", state: "offline" },
            { tag: "unsupported", state: "unsupported" },
            { tag: "no destination", state: "no-destination" },
            { tag: "error", state: "error" }
        ];
    }

    function test_status_transition_reveals_callout(data) {
        resetStatus();
        var scroll = findChild(editor, "editorScroll");
        verify(scroll !== null);
        verify(scroll.contentHeight > scroll.height);
        compare(scroll.contentY, 0);

        if (data.state === "offline")
            editor.offline = true;
        else if (data.state === "unsupported")
            editor.eventData = Object.assign({}, ownedEvent, { organizer_owned: false });
        else if (data.state === "no-destination")
            editor.calendars = [];
        else
            editor.errorText = "Event changed remotely. Your draft is preserved.";

        tryVerify(function () {
            return Math.abs(scroll.contentY - (scroll.contentHeight - scroll.height)) < 0.5;
        });
    }

    function test_unsupported_edit_primary_action_starts_duplicate() {
        resetStatus();
        duplicateSpy.clear();
        editor.eventData = Object.assign({}, ownedEvent, { organizer_owned: false });
        wait(0);

        verify(findText(editor, "Duplicate") !== null);
        editor.submit();

        compare(duplicateSpy.count, 1);
    }

    function test_delete_confirmation_still_reveals_its_actions() {
        resetStatus();
        var scroll = findChild(editor, "editorScroll");
        verify(scroll !== null);
        editor.revealDeleteConfirmation();

        tryVerify(function () {
            return Math.abs(scroll.contentY - (scroll.contentHeight - scroll.height)) < 0.5;
        });
        verify(editor.confirmDelete);
    }
}
