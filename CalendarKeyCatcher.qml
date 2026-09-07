// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick

Item {
    id: root
    objectName: "panelKeyCatcher"
    property bool blocked: false
    signal moveRequested(int dx, int dy)
    signal modifiedMoveRequested(int dx, int dy)
    signal activateRequested
    signal returnRequested
    signal closeRequested
    signal deleteRequested
    signal tabRequested(int direction)
    signal textKey(string text)
    focus: true
    Keys.priority: Keys.BeforeItem
    Keys.onPressed: function (event) {
        if (root.blocked) return;
        var shifted = Boolean(event.modifiers & Qt.ShiftModifier);
        if (shifted && event.key === Qt.Key_H) { root.modifiedMoveRequested(-1, 0); event.accepted = true; return; }
        if (shifted && event.key === Qt.Key_L) { root.modifiedMoveRequested(1, 0); event.accepted = true; return; }
        if (shifted && event.key === Qt.Key_J) { root.modifiedMoveRequested(0, 1); event.accepted = true; return; }
        if (shifted && event.key === Qt.Key_K) { root.modifiedMoveRequested(0, -1); event.accepted = true; return; }
        if (event.key === Qt.Key_Escape) { root.closeRequested(); event.accepted = true; return; }
        if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) { root.tabRequested(shifted || event.key === Qt.Key_Backtab ? -1 : 1); event.accepted = true; return; }
        if (event.key === Qt.Key_Down || event.text === "j") { root.moveRequested(0, 1); event.accepted = true; return; }
        if (event.key === Qt.Key_Up || event.text === "k") { root.moveRequested(0, -1); event.accepted = true; return; }
        if (event.key === Qt.Key_Right || event.text === "l") { root.moveRequested(1, 0); event.accepted = true; return; }
        if (event.key === Qt.Key_Left || event.text === "h") { root.moveRequested(-1, 0); event.accepted = true; return; }
        if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) { root.returnRequested(); root.activateRequested(); event.accepted = true; return; }
        if (event.key === Qt.Key_Space) { root.activateRequested(); event.accepted = true; return; }
        if (event.text === "x" || event.text === "X") { root.deleteRequested(); event.accepted = true; return; }
        if (event.text && event.text.length === 1) root.textKey(event.text);
    }
}
