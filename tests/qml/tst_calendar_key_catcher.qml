// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import QtTest
import "../.." as FlightDeck

TestCase {
    id: testCase
    name: "CalendarKeyCatcher"
    width: 240
    height: 120
    visible: true
    when: windowShown

    FlightDeck.CalendarKeyCatcher {
        id: catcher
        anchors.fill: parent
    }
    SignalSpy { id: moveSpy; target: catcher; signalName: "moveRequested" }
    SignalSpy { id: modifiedSpy; target: catcher; signalName: "modifiedMoveRequested" }

    function init() {
        moveSpy.clear();
        modifiedSpy.clear();
        catcher.forceActiveFocus();
    }

    function test_shift_j_routes_to_draft_movement_once() {
        keyClick(Qt.Key_J, Qt.ShiftModifier);
        compare(modifiedSpy.count, 1);
        compare(moveSpy.count, 0);
        compare(modifiedSpy.signalArguments[0][0], 0);
        compare(modifiedSpy.signalArguments[0][1], 1);
    }

    function test_plain_j_keeps_normal_navigation() {
        keyClick(Qt.Key_J);
        compare(moveSpy.count, 1);
        compare(modifiedSpy.count, 0);
        compare(moveSpy.signalArguments[0][0], 0);
        compare(moveSpy.signalArguments[0][1], 1);
    }
}
