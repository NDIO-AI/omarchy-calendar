// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import qs.Commons

Rectangle {
    id: root
    objectName: "copyResultOffer"

    property var palette
    property string fontFamily: ""
    property real textScale: 1
    property string reason: ""
    property string errorText: ""
    property bool canDeleteOriginal: false
    property bool needsPermission: false
    property bool confirmDelete: false
    property bool busy: false

    signal keepRequested()
    signal deleteRequested()
    signal enableEditingRequested()

    readonly property bool hasSecondaryAction: root.canDeleteOriginal || root.needsPermission

    height: Style.space(112)
    radius: Style.space(8)
    color: root.palette.surface
    border.color: root.errorText ? root.palette.urgent : root.palette.positive
    clip: true

    Text {
        textFormat: Text.PlainText
        objectName: "copyResultMessage"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Style.space(12)
        height: Style.space(38)
        text: root.errorText || root.reason || "Copy saved and verified. The original is unchanged."
        color: root.errorText ? root.palette.urgent : root.palette.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption * root.textScale
        wrapMode: Text.Wrap
        maximumLineCount: 2
        elide: Text.ElideRight
    }

    Row {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Style.space(12)
        height: Style.space(34)
        spacing: Style.space(8)

        Rectangle {
            width: root.hasSecondaryAction ? (parent.width - parent.spacing) / 2 : parent.width
            height: parent.height
            radius: Style.space(5)
            color: "transparent"
            border.color: root.palette.border
            Text {
                textFormat: Text.PlainText
                anchors.centerIn: parent
                text: "k  Keep both"
                color: root.palette.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
            }
            MouseArea {
                anchors.fill: parent
                enabled: !root.busy
                onClicked: root.keepRequested()
            }
        }

        Rectangle {
            visible: root.canDeleteOriginal || root.needsPermission
            width: visible ? (parent.width - parent.spacing) / 2 : 0
            height: parent.height
            radius: Style.space(5)
            color: root.needsPermission ? root.palette.accent : root.palette.urgent
            Text {
                textFormat: Text.PlainText
                anchors.centerIn: parent
                text: root.needsPermission ? "e  Enable editing" : root.confirmDelete ? "x  Confirm delete" : "x  Delete original"
                color: root.palette.background
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
                font.bold: true
            }
            MouseArea {
                anchors.fill: parent
                enabled: !root.busy
                onClicked: root.needsPermission ? root.enableEditingRequested() : root.deleteRequested()
            }
        }
    }
}
