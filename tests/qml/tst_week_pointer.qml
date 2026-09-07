// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtTest
import "../.." as FlightDeck
import "../../EventEditorModel.js" as EventEditorModel

TestCase {
    id: testCase
    name: "WeekPointerDraft"
    width: 1048
    height: 720
    visible: true
    when: windowShown

    readonly property var palette: ({
        background: "#16161e",
        surface: "#1f2335",
        foreground: "#c0caf5",
        muted: "#9aa5ce",
        accent: "#7aa2f7",
        border: "#3b4261",
        positive: "#9ece6a",
        urgent: "#f7768e"
    })
    readonly property var sourceEvent: ({
        uid: "google:test:personal:event",
        title: "Calendar design review",
        start: "2026-09-02T10:00:00-05:00",
        end: "2026-09-02T11:00:00-05:00",
        start_day: "2026-09-02",
        end_day: "2026-09-02",
        all_day: false,
        calendar_color: "#7aa2f7"
    })
    readonly property var allDayEvent: ({
        uid: "google:test:personal:all-day",
        title: "Release day",
        start: "2026-08-31T00:00:00-05:00",
        end: "2026-09-01T00:00:00-05:00",
        start_day: "2026-08-31",
        end_day: "2026-09-01",
        all_day: true,
        calendar_color: "#9ece6a"
    })
    property var draft: makeDraft()
    property int dragSignals: 0
    property int resizeSignals: 0
    property int emptySlotSignals: 0
    property string lastDraggedUid: ""
    property int lastDayAmount: 0
    property int lastMinuteAmount: 0
    property int lastEmptyMinute: -1

    function makeDraft() {
        return {
            source_uid: sourceEvent.uid,
            title: sourceEvent.title,
            day: sourceEvent.start_day,
            end_day: sourceEvent.end_day,
            day_span: 0,
            start: "10:00",
            end: "11:00",
            all_day: false
        };
    }

    FlightDeck.WeekView {
        id: week
        anchors.fill: parent
        events: [testCase.sourceEvent, testCase.allDayEvent]
        weekDays: [
            new Date(2026, 7, 31), new Date(2026, 8, 1), new Date(2026, 8, 2),
            new Date(2026, 8, 3), new Date(2026, 8, 4), new Date(2026, 8, 5),
            new Date(2026, 8, 6)
        ]
        selectedDay: new Date(2026, 8, 2)
        selectedUid: testCase.sourceEvent.uid
        selectedEvent: testCase.sourceEvent
        nowTime: new Date(2026, 8, 2, 16, 0, 0)
        startHour: 7
        endHour: 20
        hourHeight: 46
        palette: testCase.palette
        fontFamily: "monospace"
        textScale: 1
        motionDuration: 0
        editingDraft: testCase.draft
        editingUid: testCase.sourceEvent.uid

        onEventDragged: function (uid, dayAmount, minuteAmount) {
            testCase.dragSignals += 1;
            testCase.lastDraggedUid = uid;
            testCase.lastDayAmount = dayAmount;
            testCase.lastMinuteAmount = minuteAmount;
            if (testCase.draft && uid === testCase.sourceEvent.uid)
                testCase.draft = EventEditorModel.shift(testCase.draft, dayAmount, minuteAmount, 0);
        }
        onEventResized: function (uid, minuteAmount) {
            testCase.resizeSignals += 1;
            testCase.lastDraggedUid = uid;
            testCase.lastMinuteAmount = minuteAmount;
            if (testCase.draft && uid === testCase.sourceEvent.uid)
                testCase.draft = EventEditorModel.shift(testCase.draft, 0, 0, minuteAmount);
        }
        onEmptySlotRequested: function (day, minute) {
            testCase.emptySlotSignals += 1;
            testCase.lastEmptyMinute = minute;
        }
    }

    function init() {
        draft = makeDraft();
        dragSignals = 0;
        resizeSignals = 0;
        emptySlotSignals = 0;
        lastDraggedUid = "";
        lastDayAmount = 0;
        lastMinuteAmount = 0;
        lastEmptyMinute = -1;
        wait(50);
    }

    function findEventCard(item, uid) {
        if (item.modelData !== undefined && item.modelData !== null
                && String(item.modelData.uid || "") === uid)
            return item;
        var children = item.children || [];
        for (var index = 0; index < children.length; index++) {
            var match = findEventCard(children[index], uid);
            if (match !== null)
                return match;
        }
        return null;
    }

    function findGridFlick(item) {
        if (item.contentY !== undefined && item.contentHeight !== undefined)
            return item;
        var children = item.children || [];
        for (var index = 0; index < children.length; index++) {
            var match = findGridFlick(children[index]);
            if (match !== null)
                return match;
        }
        return null;
    }

    function assertSourceUnchanged() {
        compare(sourceEvent.start, "2026-09-02T10:00:00-05:00");
        compare(sourceEvent.end, "2026-09-02T11:00:00-05:00");
    }

    function test_current_time_spans_the_grid_and_marks_the_gutter() {
        var line = findChild(week, "currentTimeLine");
        var marker = findChild(week, "currentTimeMarker");

        verify(line !== null);
        verify(marker !== null);
        verify(line.visible);
        verify(marker.visible);
        verify(line.width >= week.width - 24);
        verify(marker.x < week.timeGutter);
    }

    function test_editing_draft_explains_move_and_resize() {
        var hint = findChild(week, "weekEditingHint");

        verify(hint !== null);
        verify(hint.visible);
        compare(hint.text, "Drag to move · Bottom edge to resize");

        draft = Object.assign({}, makeDraft(), { all_day: true });
        tryCompare(hint, "text", "Drag to move");
    }

    function test_drag_commits_one_day_to_the_local_draft() {
        var overlay = findChild(week, "eventDraftOverlay");
        verify(overlay && overlay.visible);
        var columnWidth = (week.width - week.timeGutter) / 7;

        mouseDrag(overlay, overlay.width / 2, Math.min(20, overlay.height / 2),
                  columnWidth, 0, Qt.LeftButton, Qt.NoModifier, 20);

        tryCompare(draft, "day", "2026-09-03");
        compare(draft.end_day, "2026-09-03");
        compare(draft.start, "10:00");
        compare(draft.end, "11:00");
        compare(dragSignals, 1);
        compare(resizeSignals, 0);
        assertSourceUnchanged();

        mouseDrag(overlay, overlay.width / 2, Math.min(20, overlay.height / 2),
                  columnWidth, 0, Qt.LeftButton, Qt.NoModifier, 20);
        tryCompare(draft, "day", "2026-09-04");
        compare(dragSignals, 2);
        assertSourceUnchanged();
    }

    function test_resize_commits_one_hour_to_the_local_draft() {
        var overlay = findChild(week, "eventDraftOverlay");
        verify(overlay && overlay.visible);
        var handle = findChild(overlay, "draftResizeHandle");
        verify(handle && handle.visible);

        mouseDrag(handle, handle.width / 2, handle.height / 2,
                  0, week.effectiveHourHeight, Qt.LeftButton, Qt.NoModifier, 4);

        tryCompare(draft, "end", "12:00");
        compare(draft.day, "2026-09-02");
        compare(draft.end_day, "2026-09-02");
        compare(draft.start, "10:00");
        compare(dragSignals, 0);
        compare(resizeSignals, 1);
        assertSourceUnchanged();

        mouseDrag(handle, handle.width / 2, handle.height / 2,
                  0, week.effectiveHourHeight, Qt.LeftButton, Qt.NoModifier, 4);
        tryCompare(draft, "end", "13:00");
        compare(resizeSignals, 2);
        assertSourceUnchanged();
    }

    function test_canceled_drag_keeps_the_local_draft_unchanged() {
        var overlay = findChild(week, "eventDraftOverlay");
        verify(overlay && overlay.visible);
        var handler = findChild(overlay, "draftMoveHandle");
        verify(handler !== null);
        var columnWidth = (week.width - week.timeGutter) / 7;
        var cancelPoint = null;
        function rememberGrab(transition, point) {
            if (transition === PointerDevice.GrabExclusive)
                cancelPoint = point;
        }

        handler.grabChanged.connect(rememberGrab);
        mousePress(overlay, overlay.width / 2, Math.min(20, overlay.height / 2), Qt.LeftButton);
        mouseMove(overlay, overlay.width / 2 + 40, Math.min(20, overlay.height / 2), 20, Qt.LeftButton);
        tryCompare(handler, "active", true);
        mouseMove(overlay, overlay.width / 2 + columnWidth, Math.min(20, overlay.height / 2), 20, Qt.LeftButton);
        var activated = handler.active;
        var capturedPoint = cancelPoint !== null;
        var translationBeforeCancel = handler.persistentTranslation.x;
        if (capturedPoint)
            handler.grabChanged(PointerDevice.CancelGrabExclusive, cancelPoint);
        var translationAfterCancel = handler.persistentTranslation.x;
        mouseRelease(overlay, overlay.width / 2 + columnWidth, Math.min(20, overlay.height / 2), Qt.LeftButton);
        handler.grabChanged.disconnect(rememberGrab);

        verify(activated);
        verify(capturedPoint);
        verify(translationBeforeCancel !== 0);
        compare(translationAfterCancel, 0);
        compare(draft.day, "2026-09-02");
        compare(draft.end_day, "2026-09-02");
        compare(dragSignals, 0);
        compare(resizeSignals, 0);
        assertSourceUnchanged();
    }

    function test_direct_event_drag_and_resize_emit_local_draft_requests() {
        draft = null;
        wait(50);
        var card = findEventCard(week, sourceEvent.uid);
        verify(card !== null);
        var columnWidth = (week.width - week.timeGutter) / 7;

        mouseDrag(card, card.width / 2, Math.min(12, card.height / 2),
                  columnWidth, 0, Qt.LeftButton, Qt.NoModifier, 20);
        tryCompare(testCase, "dragSignals", 1);
        compare(lastDraggedUid, sourceEvent.uid);
        compare(lastDayAmount, 1);
        compare(lastMinuteAmount, 0);
        assertSourceUnchanged();

        var handle = findChild(card, "eventResizeHandle");
        verify(handle !== null);
        mouseDrag(handle, handle.width / 2, handle.height / 2,
                  0, week.effectiveHourHeight, Qt.LeftButton, Qt.NoModifier, 4);
        tryCompare(testCase, "resizeSignals", 1);
        compare(lastDraggedUid, sourceEvent.uid);
        compare(lastMinuteAmount, 60);
        assertSourceUnchanged();
    }

    function test_all_day_event_drag_emits_one_day_request() {
        draft = null;
        wait(50);
        var card = findEventCard(week, allDayEvent.uid);
        verify(card !== null);
        var columnWidth = (week.width - week.timeGutter) / 7;

        mouseDrag(card, card.width / 2, card.height / 2,
                  columnWidth, 0, Qt.LeftButton, Qt.NoModifier, 20);
        tryCompare(testCase, "dragSignals", 1);
        compare(lastDraggedUid, allDayEvent.uid);
        compare(lastDayAmount, 1);
        compare(lastMinuteAmount, 0);
    }

    function test_empty_slot_tap_does_not_compete_with_event_tap() {
        draft = null;
        wait(50);
        var grid = findGridFlick(week);
        verify(grid !== null);
        var columnWidth = (grid.width - week.timeGutter) / 7;
        var emptyX = week.timeGutter + columnWidth * 6.5;
        var emptyY = week.topPadding + week.effectiveHourHeight - grid.contentY;

        mouseClick(grid, emptyX, emptyY, Qt.LeftButton);
        tryCompare(testCase, "emptySlotSignals", 1);
        compare(lastEmptyMinute, 8 * 60);

        var card = findEventCard(week, sourceEvent.uid);
        verify(card !== null);
        mouseClick(card, card.width / 2, card.height / 2, Qt.LeftButton);
        wait(50);
        compare(emptySlotSignals, 1);
    }

    function test_selection_strip_describes_the_active_draft() {
        var time = findChild(week, "weekSelectionTime");
        verify(time !== null);
        compare(time.text, "10:00 to 11:00");

        draft = Object.assign({}, draft, { all_day: true });
        wait(0);
        compare(time.text, "All day");
    }
}
