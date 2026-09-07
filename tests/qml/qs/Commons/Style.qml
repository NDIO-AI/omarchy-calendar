// SPDX-License-Identifier: GPL-3.0-or-later
pragma Singleton
import QtQuick

QtObject {
    readonly property var font: ({ family: "monospace", body: 12, bodySmall: 12, caption: 10, title: 14 })
    function space(value) { return Number(value); }
}
