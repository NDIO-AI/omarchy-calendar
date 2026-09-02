// SPDX-License-Identifier: GPL-3.0-or-later
import QtQuick
import qs.Commons
import "EventEditorModel.js" as EventEditorModel

Rectangle {
    id: root
    objectName: "eventEditor"

    property var draft: ({})
    property var eventData: null
    property var calendars: []
    property string mode: "create"
    property var palette: ({})
    property string fontFamily: Style.font.family
    property real textScale: 1
    property bool busy: false
    property bool offline: false
    property bool needsPermission: false
    property string errorText: ""
    property string noticeText: ""
    property int controlIndex: 0
    property int weekdayIndex: 0
    property bool confirmDelete: false
    readonly property var destinations: calendars.filter(function (item) {
        return item.writable && item.owned;
    })
    readonly property bool recurring: Boolean(eventData && eventData.recurrence_id)
    readonly property bool unsupportedEvent: root.mode === "update" && root.eventData && !root.eventData.organizer_owned
    readonly property bool noDestination: root.destinations.length === 0
    readonly property bool canSubmit: !root.busy && !root.offline && !root.unsupportedEvent && !root.noDestination
    readonly property bool inputFocused: titleInput.activeFocus || dayInput.activeFocus || startInput.activeFocus || endInput.activeFocus || locationInput.activeFocus || notesInput.activeFocus || countInput.activeFocus || untilInput.activeFocus

    signal draftUpdated(var draft)
    signal saveRequested
    signal cancelRequested
    signal deleteRequested(string scope)
    signal enableEditingRequested
    signal copyMeetingRequested

    color: palette.surface || "#171b2b"
    border.color: palette.border || "#3b4261"
    border.width: 1

    function update(key, value) {
        var next = {};
        for (var existing in root.draft)
            next[existing] = root.draft[existing];
        next[key] = value;
        root.draftUpdated(next);
    }
    function updateRecurrence(key, value) {
        var recurrence = {};
        var current = root.draft.recurrence || {};
        for (var existing in current)
            recurrence[existing] = current[existing];
        recurrence[key] = value;
        var next = {};
        for (var draftKey in root.draft)
            next[draftKey] = root.draft[draftKey];
        next.recurrence = recurrence;
        if (root.mode === "update" && root.recurring && recurrence.frequency !== "preserve")
            next.scope = "series";
        root.draftUpdated(next);
    }
    function setScope(value) {
        var next = {};
        for (var existing in root.draft)
            next[existing] = root.draft[existing];
        next.scope = value;
        if (value === "single" && root.mode === "update" && root.recurring)
            next.recurrence = { frequency: "preserve", weekdays: [], end: "never" };
        root.draftUpdated(next);
    }
    function calendarIndex() {
        for (var i = 0; i < destinations.length; i++)
            if (destinations[i].key === root.draft.calendar_key)
                return i;
        return 0;
    }
    function cycleCalendar(amount) {
        if (!destinations.length)
            return;
        var index = (calendarIndex() + Number(amount) + destinations.length) % destinations.length;
        var next = {};
        for (var existing in root.draft)
            next[existing] = root.draft[existing];
        next.calendar_key = destinations[index].key;
        if (root.eventData && next.calendar_key !== root.eventData.calendar_key && next.recurrence.frequency === "preserve")
            next.recurrence = { frequency: "none", weekdays: [], end: "never" };
        root.draftUpdated(next);
    }
    function recurrenceIndex() {
        var values = ["preserve", "none", "daily", "weekdays", "weekly", "monthly", "selected_weekdays"];
        return Math.max(0, values.indexOf(String((draft.recurrence || {}).frequency || "none")));
    }
    function cycleRecurrence(amount) {
        var values = ["preserve", "none", "daily", "weekdays", "weekly", "monthly", "selected_weekdays"];
        root.updateRecurrence("frequency", values[(recurrenceIndex() + Number(amount) + values.length) % values.length]);
    }
    function recurrenceLabel() {
        return ({
                preserve: "Keep current schedule",
                none: "Does not repeat",
                daily: "Daily",
                weekdays: "Weekdays",
                weekly: "Weekly",
                monthly: "Monthly",
                selected_weekdays: "Selected weekdays"
            })[String((draft.recurrence || {}).frequency || "none")];
    }
    function cycleEnding(amount) {
        var values = ["never", "count", "date"];
        var current = Math.max(0, values.indexOf(String((draft.recurrence || {}).end || "never")));
        root.updateRecurrence("end", values[(current + Number(amount) + values.length) % values.length]);
    }
    function meetingOptions() {
        return EventEditorModel.meetingOptions(root.draft, root.eventData, root.calendars);
    }
    function cycleMeeting(amount) {
        var values = meetingOptions();
        var current = Math.max(0, values.indexOf(String(draft.online_meeting || "none")));
        root.update("online_meeting", values[(current + Number(amount) + values.length) % values.length]);
    }
    function meetingLabel() {
        if (draft.online_meeting === "preserve")
            return "Keep existing meeting link";
        if (draft.online_meeting === "new")
            return "Generate a new meeting";
        return "No online meeting";
    }
    function moveField(amount) {
        var controls = EventEditorModel.visibleControls(root.draft, root.mode, root.recurring);
        var current = Math.max(0, controls.indexOf(controlIndex));
        var position = Math.max(0, Math.min(controls.length - 1, current + Number(amount)));
        controlIndex = controls[position];
        editorScroll.contentY = Math.max(0, Math.min(editorScroll.contentHeight - editorScroll.height, position * Style.space(54) - editorScroll.height * 0.34));
    }
    function adjustCurrent(amount) {
        if (controlIndex === 1)
            cycleCalendar(amount);
        else if (controlIndex === 2)
            root.draftUpdated(EventEditorModel.shift(root.draft, amount, 0, 0));
        else if (controlIndex === 3)
            root.draftUpdated(EventEditorModel.shift(root.draft, 0, amount * 15, 0));
        else if (controlIndex === 4)
            root.draftUpdated(EventEditorModel.shift(root.draft, 0, 0, amount * 15));
        else if (controlIndex === 5)
            toggleAllDay();
        else if (controlIndex === 8)
            cycleRecurrence(amount);
        else if (controlIndex === 9)
            weekdayIndex = (weekdayIndex + Number(amount) + 7) % 7;
        else if (controlIndex === 10)
            cycleEnding(amount);
        else if (controlIndex === 11)
            cycleMeeting(amount);
        else if (controlIndex === 12)
            root.setScope(root.draft.scope === "series" ? "single" : "series");
        else if (controlIndex === 13 && root.confirmDelete && root.recurring)
            root.setScope(root.draft.scope === "series" ? "single" : "series");
    }
    function toggleAllDay() {
        root.draftUpdated(EventEditorModel.toggleAllDay(root.draft));
    }
    function submit() {
        if (!root.canSubmit)
            return;
        root.needsPermission ? root.enableEditingRequested() : root.saveRequested();
    }
    function activateCurrent() {
        if (controlIndex === 0)
            titleInput.forceActiveFocus();
        else if (controlIndex === 1)
            cycleCalendar(1);
        else if (controlIndex === 2)
            dayInput.forceActiveFocus();
        else if (controlIndex === 3)
            startInput.forceActiveFocus();
        else if (controlIndex === 4)
            endInput.forceActiveFocus();
        else if (controlIndex === 5)
            toggleAllDay();
        else if (controlIndex === 6)
            locationInput.forceActiveFocus();
        else if (controlIndex === 7)
            notesInput.forceActiveFocus();
        else if (controlIndex === 8)
            cycleRecurrence(1);
        else if (controlIndex === 9)
            root.draftUpdated(EventEditorModel.toggleWeekday(root.draft, ["MO", "TU", "WE", "TH", "FR", "SA", "SU"][root.weekdayIndex]));
        else if (controlIndex === 10)
            (root.draft.recurrence || {}).end === "count" ? countInput.forceActiveFocus() : (root.draft.recurrence || {}).end === "date" ? untilInput.forceActiveFocus() : cycleEnding(1);
        else if (controlIndex === 11)
            cycleMeeting(1);
        else if (controlIndex === 12)
            root.setScope(root.draft.scope === "series" ? "single" : "series");
        else if (controlIndex === 13) {
            if (root.confirmDelete)
                root.deleteRequested(root.recurring ? root.draft.scope : "single");
            else
                root.confirmDelete = true;
        }
        else if (controlIndex === 14)
            cancelRequested();
        else if (controlIndex === 15)
            submit();
    }
    function handleInputKey(event) {
        if ((event.modifiers & Qt.ControlModifier) && (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)) {
            root.submit();
            event.accepted = true;
        } else if (event.key === Qt.Key_Escape) {
            root.cancelRequested();
            event.accepted = true;
        }
    }

    component FieldLabel: Text {
        textFormat: Text.PlainText
        color: root.palette.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption * root.textScale
        font.bold: true
    }

    Text {
        id: editorTitle
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: Style.space(18)
        text: root.mode === "copy" ? "COPY EVENT" : root.mode === "update" ? "EDIT EVENT" : "NEW EVENT"
        color: root.mode === "copy" ? root.palette.positive : root.palette.accent
        font.family: root.fontFamily
        font.pixelSize: Style.font.body * root.textScale
        font.bold: true
        font.letterSpacing: 1
    }

    Text {
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Style.space(18)
        text: "Close  Esc"
        color: root.palette.muted
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption * root.textScale
        MouseArea {
            anchors.fill: parent
            onClicked: root.cancelRequested()
        }
    }

    Flickable {
        id: editorScroll
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: editorTitle.bottom
        anchors.bottom: footer.top
        anchors.margins: Style.space(16)
        anchors.topMargin: Style.space(14)
        contentHeight: form.implicitHeight + Style.space(20)
        clip: true
        boundsBehavior: Flickable.StopAtBounds

        Column {
            id: form
            width: editorScroll.width
            spacing: Style.space(12)

            FieldLabel {
                text: "Title"
                color: root.controlIndex === 0 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(42)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 0 ? root.palette.accent : root.palette.border
                TextInput {
                    id: titleInput
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    text: String(root.draft.title || "")
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    selectByMouse: true
                    maximumLength: 500
                    onTextEdited: root.update("title", text)
                    Keys.onPressed: function (event) {
                        root.handleInputKey(event);
                    }
                }
                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton
                    propagateComposedEvents: true
                    onPressed: function (mouse) {
                        root.controlIndex = 0;
                        mouse.accepted = false;
                    }
                }
            }

            FieldLabel {
                text: "Calendar"
                color: root.controlIndex === 1 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(42)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 1 ? root.palette.accent : root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    verticalAlignment: Text.AlignVCenter
                    text: root.destinations.length ? String(root.destinations[root.calendarIndex()].name || "Calendar") + "  ·  " + (root.destinations[root.calendarIndex()].provider === "google" ? "Google" : "Outlook") : "No owned writable calendar"
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    elide: Text.ElideRight
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        root.controlIndex = 1;
                        root.cycleCalendar(1);
                    }
                }
            }
            Text {
                textFormat: Text.PlainText
                visible: root.mode === "copy"
                width: parent.width
                text: "Changing calendars creates and verifies a copy. The original remains unchanged."
                color: root.palette.positive
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
                wrapMode: Text.Wrap
            }

            FieldLabel {
                text: "Day"
                color: root.controlIndex === 2 ? root.palette.accent : root.palette.foreground
            }
            Row {
                width: parent.width
                height: Style.space(42)
                spacing: Style.space(6)
                Rectangle {
                    width: Style.space(44)
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.palette.border
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: "Prev"
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            root.controlIndex = 2;
                            root.draftUpdated(EventEditorModel.shift(root.draft, -1, 0, 0));
                        }
                    }
                }
                Rectangle {
                    width: parent.width - Style.space(100)
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.controlIndex === 2 ? root.palette.accent : root.palette.border
                    TextInput {
                        id: dayInput
                        anchors.fill: parent
                        anchors.margins: Style.space(10)
                        text: String(root.draft.day || "")
                        horizontalAlignment: Text.AlignHCenter
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall * root.textScale
                        inputMask: "9999-99-99"
                        onEditingFinished: root.update("day", text)
                        Keys.onPressed: function (event) {
                            root.handleInputKey(event);
                        }
                    }
                }
                Rectangle {
                    width: Style.space(44)
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.palette.border
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: "Next"
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            root.controlIndex = 2;
                            root.draftUpdated(EventEditorModel.shift(root.draft, 1, 0, 0));
                        }
                    }
                }
            }

            Row {
                width: parent.width
                spacing: Style.space(10)
                Column {
                    width: (parent.width - parent.spacing) / 2
                    spacing: Style.space(5)
                    FieldLabel {
                        text: "Start"
                        color: root.controlIndex === 3 ? root.palette.accent : root.palette.foreground
                    }
                    Rectangle {
                        width: parent.width
                        height: Style.space(42)
                        radius: Style.space(6)
                        color: "transparent"
                        border.color: root.controlIndex === 3 ? root.palette.accent : root.palette.border
                        opacity: root.draft.all_day ? 0.45 : 1
                        TextInput {
                            id: startInput
                            enabled: !root.draft.all_day
                            anchors.fill: parent
                            anchors.margins: Style.space(10)
                            text: String(root.draft.start || "")
                            horizontalAlignment: Text.AlignHCenter
                            color: root.palette.foreground
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.bodySmall * root.textScale
                            inputMask: "99:99"
                            onEditingFinished: root.update("start", text)
                            Keys.onPressed: function (event) {
                                root.handleInputKey(event);
                            }
                        }
                    }
                }
                Column {
                    width: (parent.width - parent.spacing) / 2
                    spacing: Style.space(5)
                    FieldLabel {
                        text: "End"
                        color: root.controlIndex === 4 ? root.palette.accent : root.palette.foreground
                    }
                    Rectangle {
                        width: parent.width
                        height: Style.space(42)
                        radius: Style.space(6)
                        color: "transparent"
                        border.color: root.controlIndex === 4 ? root.palette.accent : root.palette.border
                        opacity: root.draft.all_day ? 0.45 : 1
                        TextInput {
                            id: endInput
                            enabled: !root.draft.all_day
                            anchors.fill: parent
                            anchors.margins: Style.space(10)
                            text: String(root.draft.end || "")
                            horizontalAlignment: Text.AlignHCenter
                            color: root.palette.foreground
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.bodySmall * root.textScale
                            inputMask: "99:99"
                            onEditingFinished: root.update("end", text)
                            Keys.onPressed: function (event) {
                                root.handleInputKey(event);
                            }
                        }
                    }
                }
            }

            Rectangle {
                width: parent.width
                height: Style.space(38)
                radius: Style.space(6)
                color: root.controlIndex === 5 ? Qt.rgba(0.478, 0.635, 0.969, 0.12) : "transparent"
                border.color: root.controlIndex === 5 ? root.palette.accent : root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.left: parent.left
                    anchors.leftMargin: Style.space(10)
                    anchors.verticalCenter: parent.verticalCenter
                    text: "All-day"
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                }
                Rectangle {
                    anchors.right: parent.right
                    anchors.rightMargin: Style.space(10)
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(34)
                    height: Style.space(18)
                    radius: height / 2
                    color: root.draft.all_day ? root.palette.accent : root.palette.border
                    Rectangle {
                        width: Style.space(14)
                        height: width
                        radius: width / 2
                        y: Style.space(2)
                        x: root.draft.all_day ? parent.width - width - Style.space(2) : Style.space(2)
                        color: root.palette.foreground
                    }
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        root.controlIndex = 5;
                        root.toggleAllDay();
                    }
                }
            }

            FieldLabel {
                text: "Location"
                color: root.controlIndex === 6 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(42)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 6 ? root.palette.accent : root.palette.border
                TextInput {
                    id: locationInput
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    text: String(root.draft.location || "")
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    maximumLength: 500
                    onTextEdited: root.update("location", text)
                    Keys.onPressed: function (event) {
                        root.handleInputKey(event);
                    }
                }
            }

            FieldLabel {
                text: "Notes"
                color: root.controlIndex === 7 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(92)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 7 ? root.palette.accent : root.palette.border
                TextEdit {
                    id: notesInput
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    text: String(root.draft.notes || "")
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    wrapMode: TextEdit.Wrap
                    selectByMouse: true
                    onTextChanged: if (activeFocus)
                        root.update("notes", text)
                    Keys.onPressed: function (event) {
                        root.handleInputKey(event);
                    }
                }
            }

            FieldLabel {
                text: "Recurrence"
                color: root.controlIndex === 8 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(42)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 8 ? root.palette.accent : root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    verticalAlignment: Text.AlignVCenter
                    text: root.recurrenceLabel()
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        root.controlIndex = 8;
                        root.cycleRecurrence(1);
                    }
                }
            }
            Row {
                visible: String((root.draft.recurrence || {}).frequency || "none") === "selected_weekdays"
                width: parent.width
                height: visible ? Style.space(30) : 0
                spacing: Style.space(4)
                Repeater {
                    model: ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
                    Rectangle {
                        required property string modelData
                        required property int index
                        property bool selected: ((root.draft.recurrence || {}).weekdays || []).indexOf(modelData) >= 0
                        width: (form.width - Style.space(24)) / 7
                        height: Style.space(30)
                        radius: Style.space(4)
                        color: selected ? root.palette.accent : "transparent"
                        border.color: root.controlIndex === 9 && root.weekdayIndex === index ? root.palette.foreground : selected ? root.palette.accent : root.palette.border
                        Text {
                            textFormat: Text.PlainText
                            anchors.centerIn: parent
                            text: parent.modelData
                            color: parent.selected ? root.palette.background : root.palette.foreground
                            font.family: root.fontFamily
                            font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                            font.bold: true
                        }
                        MouseArea {
                            anchors.fill: parent
                            onClicked: {
                                root.controlIndex = 9;
                                root.weekdayIndex = parent.index;
                                root.draftUpdated(EventEditorModel.toggleWeekday(root.draft, parent.modelData));
                            }
                        }
                    }
                }
            }
            Row {
                property string frequency: String((root.draft.recurrence || {}).frequency || "none")
                visible: frequency !== "none" && frequency !== "preserve"
                width: parent.width
                height: visible ? Style.space(42) : 0
                spacing: Style.space(8)
                Rectangle {
                    width: parent.width * 0.46
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.controlIndex === 10 ? root.palette.accent : root.palette.border
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: "Ends: " + String((root.draft.recurrence || {}).end || "never")
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption * root.textScale
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: {
                            root.controlIndex = 10;
                            root.cycleEnding(1);
                        }
                    }
                }
                Rectangle {
                    visible: (root.draft.recurrence || {}).end === "count"
                    width: parent.width - parent.spacing - parent.children[0].width
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.palette.border
                    TextInput {
                        id: countInput
                        anchors.fill: parent
                        anchors.margins: Style.space(10)
                        text: String((root.draft.recurrence || {}).count || 1)
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption * root.textScale
                        validator: IntValidator {
                            bottom: 1
                            top: 999
                        }
                        onEditingFinished: root.updateRecurrence("count", Number(text))
                        Keys.onPressed: function (event) {
                            root.handleInputKey(event);
                        }
                    }
                }
                Rectangle {
                    visible: (root.draft.recurrence || {}).end === "date"
                    width: parent.width - parent.spacing - parent.children[0].width
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.palette.border
                    TextInput {
                        id: untilInput
                        anchors.fill: parent
                        anchors.margins: Style.space(10)
                        text: String((root.draft.recurrence || {}).until || root.draft.day || "")
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption * root.textScale
                        inputMask: "9999-99-99"
                        onEditingFinished: root.updateRecurrence("until", text)
                        Keys.onPressed: function (event) {
                            root.handleInputKey(event);
                        }
                    }
                }
            }

            FieldLabel {
                text: "Online meeting"
                color: root.controlIndex === 11 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(42)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 11 ? root.palette.accent : root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    verticalAlignment: Text.AlignVCenter
                    text: root.meetingLabel()
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    elide: Text.ElideRight
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        root.controlIndex = 11;
                        root.cycleMeeting(1);
                    }
                }
            }
            Rectangle {
                visible: Boolean(root.eventData && root.eventData.meeting_url)
                width: parent.width
                height: visible ? Style.space(34) : 0
                radius: Style.space(5)
                color: "transparent"
                border.color: root.controlIndex === 12 ? root.palette.accent : root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: "Copy meeting link"
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.copyMeetingRequested()
                }
            }
            Text {
                textFormat: Text.PlainText
                visible: root.noticeText !== ""
                width: parent.width
                height: visible ? Style.space(22) : 0
                text: root.noticeText
                color: root.palette.positive
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
                horizontalAlignment: Text.AlignHCenter
            }

            Rectangle {
                visible: root.recurring
                width: parent.width
                height: visible ? Style.space(42) : 0
                radius: Style.space(6)
                color: "transparent"
                border.color: root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: root.draft.scope === "series" ? "Entire series" : "This occurrence"
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: root.setScope(root.draft.scope === "series" ? "single" : "series")
                }
            }

            Rectangle {
                visible: root.mode === "update"
                width: parent.width
                height: visible ? Style.space(38) : 0
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 13 ? root.palette.foreground : root.palette.urgent
                Text {
                    textFormat: Text.PlainText
                    anchors.centerIn: parent
                    text: root.confirmDelete ? root.recurring ? root.draft.scope === "series" ? "Delete entire series?  Enter" : "Delete this occurrence?  Enter" : "Delete event?  Enter" : "Delete event"
                    color: root.palette.urgent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    font.bold: true
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: {
                        root.controlIndex = 13;
                        root.confirmDelete = true;
                    }
                }
            }
            Row {
                visible: root.confirmDelete
                width: parent.width
                height: visible ? Style.space(40) : 0
                spacing: Style.space(6)
                Rectangle {
                    width: (parent.width - parent.spacing) / 2
                    height: parent.height
                    radius: Style.space(6)
                    color: root.palette.urgent
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: root.recurring ? "Delete this occurrence" : "Confirm delete"
                        color: root.palette.background
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        font.bold: true
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: root.deleteRequested("single")
                    }
                }
                Rectangle {
                    visible: root.recurring
                    width: visible ? (parent.width - parent.spacing) / 2 : 0
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.palette.urgent
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: "Delete entire series"
                        color: root.palette.urgent
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        font.bold: true
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: root.deleteRequested("series")
                    }
                }
            }

            Rectangle {
                visible: root.needsPermission || root.offline || root.unsupportedEvent || root.noDestination || root.errorText !== ""
                width: parent.width
                height: visible ? permissionText.implicitHeight + Style.space(26) : 0
                radius: Style.space(6)
                color: Qt.rgba(0.97, 0.46, 0.56, 0.08)
                border.color: root.offline || root.errorText !== "" ? root.palette.urgent : root.palette.accent
                Text {
                    id: permissionText
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(12)
                    text: root.offline ? "Editing is unavailable offline. Your draft is preserved." : root.errorText !== "" ? root.errorText : root.unsupportedEvent ? "Only events you organize can be edited. Duplicate this event to create your own copy." : root.noDestination ? "No owned writable calendar is available." : "This account is still read-only. Enable Read and edit permission to save."
                    color: root.offline || root.errorText !== "" ? root.palette.urgent : root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    wrapMode: Text.Wrap
                }
                MouseArea {
                    anchors.fill: parent
                    enabled: root.needsPermission && !root.offline
                    onClicked: root.enableEditingRequested()
                }
            }
        }
    }

    Rectangle {
        objectName: "editorScrollAffordance"
        visible: editorScroll.contentHeight > editorScroll.height
        anchors.top: editorScroll.top
        anchors.right: editorScroll.right
        anchors.bottom: editorScroll.bottom
        width: Style.space(3)
        radius: width / 2
        color: Qt.rgba(0.59, 0.65, 0.81, 0.12)

        Rectangle {
            width: parent.width
            height: Math.max(Style.space(28), parent.height * editorScroll.height / editorScroll.contentHeight)
            y: (parent.height - height) * editorScroll.visibleArea.yPosition / Math.max(0.001, 1 - editorScroll.visibleArea.heightRatio)
            radius: width / 2
            color: root.palette.accent
            opacity: 0.65
        }
    }

    Row {
        id: footer
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.margins: Style.space(16)
        height: Style.space(44)
        spacing: Style.space(8)
        Rectangle {
            width: (parent.width - parent.spacing) * 0.4
            height: parent.height
            radius: Style.space(6)
            color: "transparent"
            border.color: root.controlIndex === 14 ? root.palette.accent : root.palette.border
            Text {
                textFormat: Text.PlainText
                anchors.centerIn: parent
                text: "Cancel  Esc"
                color: root.palette.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
                font.bold: true
            }
            MouseArea {
                anchors.fill: parent
                onClicked: root.cancelRequested()
            }
        }
        Rectangle {
            width: (parent.width - parent.spacing) * 0.6
            height: parent.height
            radius: Style.space(6)
            border.color: root.controlIndex === 15 ? root.palette.foreground : root.palette.accent
            color: root.canSubmit ? root.palette.accent : root.palette.border
            Text {
                textFormat: Text.PlainText
                anchors.centerIn: parent
                text: root.busy ? "Saving" : "Save  Ctrl+Enter"
                color: root.canSubmit ? root.palette.background : root.palette.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
                font.bold: true
            }
            MouseArea {
                anchors.fill: parent
                enabled: root.canSubmit
                onClicked: root.submit()
            }
        }
    }
}
