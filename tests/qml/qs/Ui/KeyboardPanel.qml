// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick

Item {
    property Item anchorItem: null
    property var owner: null
    property var bar: null
    property bool open: true
    property bool centerOnBar: false
    property Item focusTarget: null
    property int contentWidth: 1080
    property int contentHeight: 720
    default property alias contentItem: holder.children
    function fittedContentWidth(value) { return value }
    function fittedContentHeight(value) { return value }
    Item { id: holder; anchors.fill: parent }
}
