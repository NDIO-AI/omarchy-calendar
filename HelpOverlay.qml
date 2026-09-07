// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import qs.Commons

Rectangle {
  id: root
  objectName: "shortcutLegend"

  property var palette: ({})
  property string fontFamily: Style.font.family
  property real textScale: 1

  color: palette.background || "#16161e"
  radius: Style.space(12)
  border.color: palette.accent || "#7aa2f7"
  border.width: 1

  readonly property var groups: [
    { title: "Calendar", rows: [["t / w", "Today or Week"], ["h / l", "Overlap lane, then day"], ["j / k", "Up or down through events"], ["[ / ]", "Previous or next period"], ["g", "Current or next event"], ["Enter", "Toggle details"], ["m", "Join meeting"], ["o", "Open source event"]] },
    { title: "Settings and accounts", rows: [["r", "Refresh calendars"], ["?", "Toggle help"], ["s", "Open Settings"], ["c", "Open Calendars"], ["h / l", "Move between sections"], ["j / k", "Move between controls"], ["Enter / Space", "Activate control"], ["a", "Apply settings"]] },
    { title: "Editing", rows: [["n / e / d", "New, edit, or duplicate"], ["Shift+H / L", "Move draft one day"], ["Shift+J / K", "Move draft 15 minutes"], ["j / k", "Move between editor fields"], ["h / l", "Change the field value"], ["Enter", "Choose recurrence scope or delete"], ["Ctrl+Enter", "Save the draft"], ["Esc", "Cancel the draft"]] },
    { title: "Copy result", rows: [["k", "Keep both copies"], ["e", "Enable source editing"], ["x", "Delete or confirm original"], ["Esc", "Keep both and close"]] },
  ]

  Column {
    anchors.fill: parent
    anchors.margins: Style.space(20)
    spacing: Style.space(14)

    Text {

      textFormat: Text.PlainText
      text: "KEYBOARD MAP"
      color: root.palette.accent
      font.family: root.fontFamily
      font.pixelSize: Style.font.title * root.textScale
      font.bold: true
      font.letterSpacing: 1
    }

    Grid {
      id: helpGrid
      width: parent.width
      columns: 2
      columnSpacing: Style.space(16)
      rowSpacing: Style.space(12)

      Repeater {
        model: root.groups
        Rectangle {
          required property var modelData
          width: (helpGrid.width - helpGrid.columnSpacing) / 2
          height: Style.space(164)
          color: root.palette.surface
          radius: Style.space(8)
          border.color: root.palette.border
          border.width: 1

          Column {
            anchors.fill: parent
            anchors.margins: Style.space(6)
            spacing: Style.space(1)
            Text {
              textFormat: Text.PlainText
              text: modelData.title.toUpperCase()
              color: root.palette.accent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption * root.textScale
              font.bold: true
            }
            Repeater {
              model: modelData.rows
              Row {
                required property var modelData
                width: parent.width
                height: Style.space(16)
                spacing: Style.space(8)
                Rectangle {
                  width: Style.space(96)
                  height: parent.height
                  radius: Style.space(4)
                  color: Qt.rgba(1, 1, 1, 0.05)
                  border.color: root.palette.border
                  border.width: 1
                  Text {
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: String(modelData[0])
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                    font.bold: true
                  }
                }
                Text {
                  textFormat: Text.PlainText
                  width: parent.width - Style.space(104)
                  text: String(modelData[1])
                  color: root.palette.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption * root.textScale
                  elide: Text.ElideRight
                }
              }
            }
          }
        }
      }
    }
  }
}
