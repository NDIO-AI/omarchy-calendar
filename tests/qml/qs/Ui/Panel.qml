// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick

Item {
    property var bar: null
    property string moduleName: ""
    property var settings: ({})
    property string ipcTarget: ""
    property bool manageIpc: false
    property alias controller: panelController
    readonly property bool opened: panelController.open
    QtObject {
        id: panelController
        property bool open: true
        function show() { open = true }
        function hide() { open = false }
    }
}
