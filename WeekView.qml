// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import qs.Commons
import "CalendarModel.js" as CalendarModel

Item {
    id: root
    objectName: "weekTimeGrid"

    property var events: []
    property var weekDays: []
    property date selectedDay: new Date()
    property string selectedUid: ""
    property var selectedEvent: null
    property date nowTime: new Date()
    property int startHour: 7
    property int endHour: 20
    property int hourHeight: Style.space(52)
    property var palette: ({})
    property string fontFamily: Style.font.family
    property real textScale: 1
    property int motionDuration: 140
    property string editAction: "edit"
    property var editingDraft: null
    property string editingUid: ""
    signal eventSelected(string uid, date day)
    signal emptySlotRequested(date day, int minute)
    signal eventDragged(string uid, int dayAmount, int minuteAmount)
    signal eventResized(string uid, int minuteAmount)
    signal meetingRequested
    signal sourceRequested
    signal editRequested
    signal duplicateRequested

    readonly property int timeGutter: Style.space(58)
    readonly property int topPadding: Style.space(14)
    readonly property real effectiveHourHeight: CalendarModel.fillHourHeight(hourHeight, gridFlick.height, endHour - startHour, topPadding)
    readonly property real gridHeight: (endHour - startHour) * effectiveHourHeight
    readonly property string overlapPosition: CalendarModel.overlapPosition(events, selectedDay, selectedUid)

    function allDayFor(day) {
        return CalendarModel.eventsForDay(events, day).filter(function (event) {
            return event.all_day;
        });
    }
    function timedFor(day) {
        return CalendarModel.overlapColumns(CalendarModel.eventsForDay(events, day).filter(function (event) {
            return !event.all_day;
        }));
    }
    function isCurrentWeek() {
        if (weekDays.length === 0)
            return false;
        return CalendarModel.dayKey(CalendarModel.startOfWeek(weekDays[0])) === CalendarModel.dayKey(CalendarModel.startOfWeek(nowTime));
    }
    function currentTimeY() {
        return topPadding + ((nowTime.getHours() + nowTime.getMinutes() / 60) - startHour) * effectiveHourHeight;
    }
    function dayIndex(value) {
        for (var i = 0; i < weekDays.length; i++)
            if (CalendarModel.dayKey(weekDays[i]) === String(value || ""))
                return i;
        return -1;
    }
    function minuteAt(value) {
        var minute = startHour * 60 + Math.round(((Number(value) - topPadding) * 60 / effectiveHourHeight) / 15) * 15;
        return Math.max(startHour * 60, Math.min(endHour * 60 - 15, minute));
    }
    function draftY() {
        if (!editingDraft || editingDraft.all_day)
            return 0;
        var parts = String(editingDraft.start || "00:00").split(":");
        return topPadding + (Number(parts[0]) * 60 + Number(parts[1]) - startHour * 60) * effectiveHourHeight / 60;
    }
    function draftHeight() {
        if (!editingDraft || editingDraft.all_day)
            return Style.space(24);
        var start = String(editingDraft.start || "00:00").split(":");
        var end = String(editingDraft.end || "00:15").split(":");
        var span = Number(editingDraft.day_span || 0) * 1440;
        return Math.max(Style.space(24), (span + (Number(end[0]) * 60 + Number(end[1])) - (Number(start[0]) * 60 + Number(start[1]))) * effectiveHourHeight / 60);
    }
    function centerCurrentTime() {
        if (!visible || !isCurrentWeek())
            return;
        var target = Math.max(0, Math.min(gridFlick.contentHeight - gridFlick.height, currentTimeY() - gridFlick.height * 0.36));
        gridFlick.contentY = target;
    }
    function revealSelectedEvent() {
        if (!visible || !selectedEvent || selectedEvent.all_day)
            return;
        var eventY = topPadding + CalendarModel.timePosition(selectedEvent, effectiveHourHeight, startHour, selectedDay);
        var eventHeight = CalendarModel.durationHeight(selectedEvent, effectiveHourHeight, selectedDay);
        gridFlick.contentY = CalendarModel.revealOffset(gridFlick.contentY, gridFlick.height, eventY, eventHeight, gridFlick.contentHeight, Style.space(56));
    }
    function showNow() {
        centerCurrentTime();
        Qt.callLater(revealSelectedEvent);
    }

    onVisibleChanged: if (visible)
        Qt.callLater(centerCurrentTime)
    onSelectedUidChanged: Qt.callLater(revealSelectedEvent)
    Component.onCompleted: Qt.callLater(centerCurrentTime)

    Column {
        id: weekLayout
        anchors.fill: parent
        anchors.margins: Style.space(6)
        spacing: Style.space(5)

        Row {
            id: weekHeader
            objectName: "weekDayHeader"
            width: parent.width
            height: Style.space(38)
            Item {
                width: root.timeGutter
                height: 1
            }
            Repeater {
                model: 7
                Rectangle {
                    required property int index
                    width: (weekLayout.width - root.timeGutter) / 7
                    height: parent.height
                    radius: Style.space(6)
                    property date day: root.weekDays[index] || new Date()
                    property bool selected: CalendarModel.dayKey(day) === CalendarModel.dayKey(root.selectedDay)
                    property bool today: CalendarModel.dayKey(day) === CalendarModel.dayKey(root.nowTime)
                    color: selected ? Qt.rgba(0.478, 0.635, 0.969, 0.22) : today ? Qt.rgba(0.478, 0.635, 0.969, 0.09) : "transparent"
                    border.color: selected ? root.palette.accent : "transparent"
                    border.width: 1
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: Qt.formatDate(parent.day, "ddd  d")
                        color: parent.selected || parent.today ? root.palette.accent : root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall * root.textScale
                        font.bold: parent.selected || parent.today
                    }
                }
            }
        }

        Row {
            id: allDayLane
            objectName: "allDayLane"
            width: parent.width
            height: Style.space(52)
            Item {
                width: root.timeGutter
                height: parent.height
                Text {
                    textFormat: Text.PlainText
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(8)
                    anchors.verticalCenter: parent.verticalCenter
                    text: "ALL DAY"
                    color: root.palette.muted
                    font.family: root.fontFamily
                    font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                }
            }
            Repeater {
                model: 7
                Rectangle {
                    id: allDayCell
                    required property int index
                    property date day: root.weekDays[index] || new Date()
                    property var dayEvents: root.allDayFor(day)
                    width: (weekLayout.width - root.timeGutter) / 7
                    height: parent.height
                    color: "transparent"
                    border.color: root.palette.border
                    border.width: 1
                    clip: true
                    Column {
                        anchors.fill: parent
                        anchors.margins: Style.space(3)
                        spacing: Style.space(2)
                        Repeater {
                            model: allDayCell.dayEvents.slice(0, 2)
                            Rectangle {
                                required property var modelData
                                property date eventDay: CalendarModel.eventDay(modelData)
                                width: parent.width
                                height: Style.space(20)
                                radius: Style.space(4)
                                color: root.selectedUid === String(modelData.uid || "") ? Qt.rgba(0.478, 0.635, 0.969, 0.34) : Qt.rgba(0.478, 0.635, 0.969, 0.16)
                                border.color: root.selectedUid === String(modelData.uid || "") ? root.palette.accent : modelData.calendar_color || root.palette.border
                                border.width: 1
                                Text {
                                    textFormat: Text.PlainText
                                    anchors.fill: parent
                                    anchors.leftMargin: Style.space(4)
                                    anchors.rightMargin: Style.space(4)
                                    text: String(modelData.title || "Untitled event")
                                    verticalAlignment: Text.AlignVCenter
                                    color: root.palette.foreground
                                    font.family: root.fontFamily
                                    font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                                    elide: Text.ElideRight
                                }
                                TapHandler {
                                    onTapped: root.eventSelected(String(modelData.uid || ""), parent.eventDay)
                                }
                                DragHandler {
                                    target: null
                                    onGrabChanged: function (transition) {
                                        if (transition === PointerDevice.CancelGrabExclusive) {
                                            persistentTranslation = Qt.vector2d(0, 0);
                                            return;
                                        }
                                        if (transition !== PointerDevice.UngrabExclusive || persistentTranslation.x === 0)
                                            return;
                                        var dragX = persistentTranslation.x;
                                        persistentTranslation = Qt.vector2d(0, 0);
                                        root.eventDragged(String(modelData.uid || ""), Math.round(dragX / allDayCell.width), 0);
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        visible: root.editingDraft && root.editingDraft.all_day && root.dayIndex(root.editingDraft.day) === allDayCell.index
                        anchors.fill: parent
                        anchors.margins: Style.space(3)
                        radius: Style.space(4)
                        color: Qt.rgba(0.478, 0.635, 0.969, 0.34)
                        border.color: root.palette.accent
                        border.width: 2
                        z: 10
                        Text {
                            textFormat: Text.PlainText
                            anchors.fill: parent
                            anchors.margins: Style.space(5)
                            text: root.editingDraft ? String(root.editingDraft.title || "New all-day event") : "New all-day event"
                            color: root.palette.foreground
                            font.family: root.fontFamily
                            font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                            elide: Text.ElideRight
                        }
                        DragHandler {
                            target: null
                            onGrabChanged: function (transition) {
                                if (transition === PointerDevice.CancelGrabExclusive) {
                                    persistentTranslation = Qt.vector2d(0, 0);
                                    return;
                                }
                                if (transition !== PointerDevice.UngrabExclusive || persistentTranslation.x === 0)
                                    return;
                                var dragX = persistentTranslation.x;
                                persistentTranslation = Qt.vector2d(0, 0);
                                root.eventDragged(root.editingUid, Math.round(dragX / allDayCell.width), 0);
                            }
                        }
                    }
                }
            }
        }

        Flickable {
            id: gridFlick
            width: parent.width
            height: Math.max(Style.space(120), parent.height - weekHeader.height - allDayLane.height - selectionStrip.height - weekLayout.spacing * 3)
            contentHeight: root.gridHeight + root.topPadding * 2
            clip: true
            boundsBehavior: Flickable.StopAtBounds

            Row {
                width: gridFlick.width
                height: gridFlick.contentHeight

                Item {
                    width: root.timeGutter
                    height: parent.height
                    Repeater {
                        model: root.endHour - root.startHour + 1
                        Text {
                            textFormat: Text.PlainText
                            required property int index
                            y: root.topPadding + index * root.effectiveHourHeight - height / 2
                            width: parent.width - Style.space(8)
                            text: String(root.startHour + index).padStart(2, "0") + ":00"
                            horizontalAlignment: Text.AlignRight
                            color: root.palette.muted
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption * root.textScale
                        }
                    }
                }

                Repeater {
                    model: 7
                    Rectangle {
                        id: timedColumn
                        required property int index
                        property date day: root.weekDays[index] || new Date()
                        property var timedEvents: root.timedFor(day)
                        width: (weekLayout.width - root.timeGutter) / 7
                        height: gridFlick.contentHeight
                        color: CalendarModel.dayKey(day) === CalendarModel.dayKey(root.selectedDay) ? Qt.rgba(0.478, 0.635, 0.969, 0.05) : "transparent"
                        border.color: root.palette.border
                        border.width: 1

                        TapHandler {
                            acceptedButtons: Qt.LeftButton
                            onTapped: function (eventPoint) {
                                var minute = root.minuteAt(eventPoint.position.y);
                                var minimumDuration = Math.ceil(Style.space(24) * 60 / root.effectiveHourHeight);
                                if (!CalendarModel.eventAtMinute(timedColumn.timedEvents, minute, minimumDuration, timedColumn.day))
                                    root.emptySlotRequested(timedColumn.day, minute);
                            }
                        }

                        Repeater {
                            model: root.endHour - root.startHour + 1
                            Rectangle {
                                required property int index
                                y: root.topPadding + index * root.effectiveHourHeight
                                width: parent.width
                                height: 1
                                color: root.palette.border
                                opacity: 0.72
                            }
                        }

                        Repeater {
                            model: timedColumn.timedEvents
                            Rectangle {
                                required property var modelData
                                property date eventDay: CalendarModel.eventDay(modelData)
                                property real available: timedColumn.width - Style.space(8)
                                x: Style.space(4) + modelData.column * available / modelData.columns
                                y: root.topPadding + CalendarModel.timePosition(modelData, root.effectiveHourHeight, root.startHour, timedColumn.day)
                                width: Math.max(Style.space(18), available / modelData.columns - Style.space(3))
                                height: Math.max(Style.space(24), CalendarModel.durationHeight(modelData, root.effectiveHourHeight, timedColumn.day))
                                radius: Style.space(5)
                                color: root.selectedUid === String(modelData.uid || "") ? Qt.rgba(0.478, 0.635, 0.969, 0.34) : Qt.rgba(0.478, 0.635, 0.969, 0.16)
                                border.color: root.selectedUid === String(modelData.uid || "") ? root.palette.accent : modelData.calendar_color || root.palette.border
                                border.width: 1
                                clip: true
                                opacity: root.editingUid === String(modelData.uid || "") ? 0.28 : 1
                                Behavior on color {
                                    enabled: root.motionDuration > 0
                                    ColorAnimation {
                                        duration: root.motionDuration
                                    }
                                }

                                Column {
                                    anchors.fill: parent
                                    anchors.margins: Style.space(5)
                                    spacing: 1
                                    Text {
                                        textFormat: Text.PlainText
                                        width: parent.width
                                        text: String(modelData.title || "Untitled event")
                                        color: root.palette.foreground
                                        font.family: root.fontFamily
                                        font.pixelSize: Style.font.caption * root.textScale
                                        font.bold: true
                                        wrapMode: Text.Wrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                    }
                                    Text {
                                        textFormat: Text.PlainText
                                        width: parent.width
                                        visible: parent.parent.height >= Style.space(44)
                                        text: CalendarModel.formatTime(modelData).split(" to ")[0]
                                        color: root.palette.muted
                                        font.family: root.fontFamily
                                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                                        elide: Text.ElideRight
                                    }
                                }
                                TapHandler {
                                    onTapped: root.eventSelected(String(modelData.uid || ""), parent.eventDay)
                                }
                                DragHandler {
                                    id: moveHandler
                                    target: null
                                    onGrabChanged: function (transition) {
                                        if (transition === PointerDevice.CancelGrabExclusive) {
                                            persistentTranslation = Qt.vector2d(0, 0);
                                            return;
                                        }
                                        if (transition !== PointerDevice.UngrabExclusive || (persistentTranslation.x === 0 && persistentTranslation.y === 0))
                                            return;
                                        var dragX = persistentTranslation.x;
                                        var dragY = persistentTranslation.y;
                                        persistentTranslation = Qt.vector2d(0, 0);
                                        root.eventDragged(String(modelData.uid || ""), Math.round(dragX / timedColumn.width), Math.round((dragY * 60 / root.effectiveHourHeight) / 15) * 15);
                                    }
                                }
                                Rectangle {
                                    objectName: "eventResizeHandle"
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.bottom: parent.bottom
                                    height: Style.space(8)
                                    color: "transparent"
                                    z: 12
                                    DragHandler {
                                        target: null
                                        xAxis.enabled: false
                                        onGrabChanged: function (transition) {
                                            if (transition === PointerDevice.CancelGrabExclusive) {
                                                persistentTranslation = Qt.vector2d(0, 0);
                                                return;
                                            }
                                            if (transition !== PointerDevice.UngrabExclusive || persistentTranslation.y === 0)
                                                return;
                                            var dragY = persistentTranslation.y;
                                            persistentTranslation = Qt.vector2d(0, 0);
                                            root.eventResized(String(modelData.uid || ""), Math.round((dragY * 60 / root.effectiveHourHeight) / 15) * 15);
                                        }
                                    }
                                }
                            }
                        }

                    }
                }
            }

            Rectangle {
                objectName: "currentTimeLine"
                visible: root.isCurrentWeek() && root.currentTimeY() >= root.topPadding && root.currentTimeY() <= root.gridHeight + root.topPadding
                x: 0
                y: root.currentTimeY()
                width: gridFlick.width
                height: 2
                color: root.palette.urgent
                z: 15
            }
            Rectangle {
                objectName: "currentTimeMarker"
                visible: root.isCurrentWeek() && root.currentTimeY() >= root.topPadding && root.currentTimeY() <= root.gridHeight + root.topPadding
                x: Style.space(2)
                y: root.currentTimeY() - height / 2
                width: Style.space(7)
                height: width
                radius: width / 2
                color: root.palette.urgent
                z: 16
            }

            Rectangle {
                visible: gridFlick.contentHeight > gridFlick.height
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: Style.space(3)
                color: Qt.rgba(1, 1, 1, 0.08)
                Rectangle {
                    width: parent.width
                    height: Math.max(Style.space(28), parent.height * gridFlick.height / gridFlick.contentHeight)
                    y: (parent.height - height) * gridFlick.contentY / (gridFlick.contentHeight - gridFlick.height)
                    color: root.palette.accent
                }
            }

            Rectangle {
                id: draftOverlay
                objectName: "eventDraftOverlay"
                visible: root.editingDraft && !root.editingDraft.all_day && root.dayIndex(root.editingDraft.day) >= 0
                x: root.timeGutter + root.dayIndex(root.editingDraft ? root.editingDraft.day : "") * (gridFlick.width - root.timeGutter) / 7 + Style.space(4)
                y: root.draftY()
                width: (gridFlick.width - root.timeGutter) / 7 - Style.space(8)
                height: root.draftHeight()
                radius: Style.space(5)
                color: Qt.rgba(0.478, 0.635, 0.969, 0.34)
                border.color: root.palette.accent
                border.width: 2
                z: 20
                DragHandler {
                    objectName: "draftMoveHandle"
                    target: null
                    onGrabChanged: function (transition) {
                        if (transition === PointerDevice.CancelGrabExclusive) {
                            persistentTranslation = Qt.vector2d(0, 0);
                            return;
                        }
                        if (transition !== PointerDevice.UngrabExclusive || (persistentTranslation.x === 0 && persistentTranslation.y === 0))
                            return;
                        var dragX = persistentTranslation.x;
                        var dragY = persistentTranslation.y;
                        persistentTranslation = Qt.vector2d(0, 0);
                        root.eventDragged(root.editingUid, Math.round(dragX / ((gridFlick.width - root.timeGutter) / 7)), Math.round((dragY * 60 / root.effectiveHourHeight) / 15) * 15);
                    }
                }
                Text {
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(6)
                    text: root.editingDraft ? String(root.editingDraft.title || "New event") : ""
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    font.bold: true
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                }
                Repeater {
                    model: [
                        { left: true, top: true },
                        { left: false, top: true },
                        { left: true, top: false },
                        { left: false, top: false }
                    ]
                    Rectangle {
                        required property var modelData
                        width: Style.space(6)
                        height: width
                        x: modelData.left ? -width / 2 : draftOverlay.width - width / 2
                        y: modelData.top ? -height / 2 : draftOverlay.height - height / 2
                        color: root.palette.foreground
                        border.color: root.palette.accent
                        border.width: 1
                    }
                }
                Rectangle {
                    objectName: "draftResizeHandle"
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: Style.space(10)
                    color: "transparent"
                    z: 25
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        anchors.bottom: parent.bottom
                        anchors.bottomMargin: -height / 2
                        width: Style.space(14)
                        height: Style.space(4)
                        radius: height / 2
                        color: root.palette.foreground
                        border.color: root.palette.accent
                        border.width: 1
                    }
                    DragHandler {
                        target: null
                        xAxis.enabled: false
                        onGrabChanged: function (transition) {
                            if (transition === PointerDevice.CancelGrabExclusive) {
                                persistentTranslation = Qt.vector2d(0, 0);
                                return;
                            }
                            if (transition !== PointerDevice.UngrabExclusive || persistentTranslation.y === 0)
                                return;
                            var dragY = persistentTranslation.y;
                            persistentTranslation = Qt.vector2d(0, 0);
                            root.eventResized(root.editingUid, Math.round((dragY * 60 / root.effectiveHourHeight) / 15) * 15);
                        }
                    }
                }
            }
        }

        Rectangle {
            id: selectionStrip
            objectName: "weekSelectionStrip"
            width: parent.width
            height: Style.space(30)
            radius: Style.space(7)
            color: root.palette.surface
            border.color: root.palette.border
            Row {
                objectName: "weekSelectionInfo"
                anchors.fill: parent
                anchors.margins: Style.space(4)
                spacing: Style.space(8)
                Text {
                    textFormat: Text.PlainText
                    objectName: "weekSelectionTime"
                    width: parent.width * 0.2
                    height: parent.height
                    text: root.editingDraft ? root.editingDraft.all_day ? "All day" : String(root.editingDraft.start || "") + " to " + String(root.editingDraft.end || "") : root.selectedEvent ? CalendarModel.formatTime(root.selectedEvent) : "No selection"
                    color: root.palette.accent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    font.bold: true
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                Text {
                    textFormat: Text.PlainText
                    objectName: "weekEditingHint"
                    width: parent.width * (root.overlapPosition ? 0.27 : 0.43)
                    height: parent.height
                    text: root.editingDraft ? (root.editingDraft.all_day ? "Drag to move" : "Drag to move · Bottom edge to resize") : root.selectedEvent ? String(root.selectedEvent.title || "Untitled event") : "Use j and k to select"
                    color: root.editingDraft ? root.palette.accent : root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    font.bold: true
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                Text {
                    textFormat: Text.PlainText
                    visible: Boolean(root.overlapPosition)
                    width: visible ? parent.width * 0.16 : 0
                    height: parent.height
                    text: root.overlapPosition ? "Overlap " + root.overlapPosition : ""
                    color: root.palette.accent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    font.bold: true
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    elide: Text.ElideRight
                }
                Row {
                    width: parent.width * 0.32
                    height: parent.height
                    spacing: Style.space(8)
                    layoutDirection: Qt.RightToLeft
                    Text {
                        textFormat: Text.PlainText
                        height: parent.height
                        text: "d Duplicate"
                        color: root.selectedEvent ? root.palette.foreground : root.palette.muted
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        verticalAlignment: Text.AlignVCenter
                        MouseArea {
                            anchors.fill: parent
                            enabled: Boolean(root.selectedEvent)
                            onClicked: root.duplicateRequested()
                        }
                    }
                    Text {
                        textFormat: Text.PlainText
                        height: parent.height
                        text: root.editAction === "enable" ? "e Enable editing" : root.editAction === "cannot" ? "Cannot edit" : "e Edit"
                        color: root.selectedEvent ? root.palette.accent : root.palette.muted
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        verticalAlignment: Text.AlignVCenter
                        MouseArea {
                            anchors.fill: parent
                            enabled: Boolean(root.selectedEvent)
                            onClicked: root.editRequested()
                        }
                    }
                    Text {
                        textFormat: Text.PlainText
                        height: parent.height
                        text: "o Source"
                        color: root.selectedEvent && root.selectedEvent.provider_url ? root.palette.foreground : root.palette.muted
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        verticalAlignment: Text.AlignVCenter
                        MouseArea {
                            anchors.fill: parent
                            enabled: Boolean(root.selectedEvent && root.selectedEvent.provider_url)
                            onClicked: root.sourceRequested()
                        }
                    }
                    Text {
                        textFormat: Text.PlainText
                        visible: Boolean(root.selectedEvent && root.selectedEvent.meeting_url)
                        height: parent.height
                        text: "m Join"
                        color: root.palette.positive
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        verticalAlignment: Text.AlignVCenter
                        MouseArea {
                            anchors.fill: parent
                            onClicked: root.meetingRequested()
                        }
                    }
                }
            }
        }
    }
}
