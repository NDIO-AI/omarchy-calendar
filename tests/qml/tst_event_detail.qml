// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtTest
import "../.." as FlightDeck

TestCase {
    id: testCase
    name: "EventDetailBounds"
    width: 420
    height: 720
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
    property var event: ({
        uid: "google:test:personal:event",
        title: "A deliberately long event title that must remain inside the detail panel",
        start: "2026-09-03T10:00:00-05:00",
        end: "2026-09-03T11:00:00-05:00",
        calendar_name: "Personal",
        provider: "google",
        account_label: "owner@example.com",
        location: "A deliberately long location that must remain inside the detail panel",
        description: "A deliberately long description that must remain inside the detail panel.",
        meeting_url: "",
        provider_url: ""
    })

    FlightDeck.EventDetail {
        id: detail
        anchors.fill: parent
        eventData: testCase.event
        palette: testCase.colors
        fontFamily: "monospace"
        textScale: 1.25
    }

    function assertBounded(name, expectedText) {
        var label = findChild(detail, name);
        verify(label !== null, name + " must exist");
        compare(label.text, expectedText);
        verify(label.width <= label.parent.width - 12);
        verify(label.paintedWidth <= label.width);
    }

    function test_disabled_action_labels_stay_inside_their_buttons() {
        wait(0);
        assertBounded("meetingActionLabel", "No meeting");
        assertBounded("sourceActionLabel", "No source");
        assertBounded("editActionLabel", "e  Edit");
        assertBounded("copyActionLabel", "d  Duplicate");
    }

    function test_enabled_action_labels_stay_inside_their_buttons() {
        testCase.event = Object.assign({}, testCase.event, {
            meeting_url: "https://meet.google.com/abc-defg-hij",
            provider_url: "https://calendar.google.com/calendar/event?eid=test"
        });
        wait(0);
        assertBounded("meetingActionLabel", "m  Join");
        assertBounded("sourceActionLabel", "o  Source");
    }

    function test_edit_action_matches_the_permission_decision() {
        verify(typeof detail.editAction === "string");
        detail.editAction = "enable";
        assertBounded("editActionLabel", "e  Enable editing");
        detail.editAction = "cannot";
        assertBounded("editActionLabel", "Cannot edit");
        detail.editAction = "edit";
        assertBounded("editActionLabel", "e  Edit");
    }
}
