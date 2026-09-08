// SPDX-License-Identifier: GPL-3.0-or-later
pragma Singleton
import QtQuick

QtObject {
    readonly property color foreground: "#c0caf5"
    readonly property color background: "#16161e"
    readonly property color accent: "#7aa2f7"
    readonly property color urgent: "#f7768e"
    readonly property color muted: "#9aa5ce"
    readonly property QtObject popups: QtObject {
        readonly property color background: "#1f2335"
        readonly property color border: "#3b4261"
    }
}
