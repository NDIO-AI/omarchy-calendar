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
    property string errorText: ""
    property string noticeText: ""
    property int controlIndex: 0
    property int weekdayIndex: 0
    property bool confirmDelete: false
    readonly property var destinations: calendars.filter(function (item) {
        return item.writable && item.owned && item.sync_enabled !== false;
    })
    readonly property bool recurring: Boolean(eventData && (eventData.recurrence_id || eventData.event_type === "series" || (eventData.recurrence || []).length))
    readonly property bool seriesOnly: root.draft.series_only === true
    readonly property bool hasMeeting: Boolean(eventData && eventData.meeting_url)
    readonly property bool sourceDestinationAvailable: root.destinations.some(function (item) { return item.key === root.draft.calendar_key; })
    readonly property bool unsupportedEvent: root.mode === "update" && root.eventData && (!root.eventData.organizer_owned || !root.sourceDestinationAvailable || root.eventData.has_attendees)
    readonly property string unsupportedReason: root.eventData && root.eventData.has_attendees ? "Events with attendees are duplicate-only in this release. Duplicate it to create your own copy." : !root.sourceDestinationAvailable ? "This event's calendar is not an owned writable destination. Duplicate it to another calendar." : "Only events you organize can be edited. Duplicate this event to create your own copy."
    readonly property bool noDestination: root.destinations.length === 0
    readonly property bool canSubmit: !root.busy && !root.offline && !root.unsupportedEvent && !root.noDestination
    readonly property bool canPrimaryAction: root.unsupportedEvent ? !root.busy && !root.offline && !root.noDestination : root.canSubmit
    readonly property bool canDelete: root.mode === "update" && !root.busy && !root.offline && !root.unsupportedEvent && !root.noDestination
    readonly property bool inputFocused: titleInput.activeFocus || dayInput.activeFocus || startInput.activeFocus || endInput.activeFocus || locationInput.activeFocus || notesInput.activeFocus || countInput.activeFocus || untilInput.activeFocus

    signal draftUpdated(var draft)
    signal saveRequested
    signal cancelRequested
    signal deleteRequested(string scope)
    signal duplicateRequested
    signal copyMeetingRequested
    signal inputNavigationRequested(int direction)

    Shortcut {
        sequences: ["Ctrl+Return", "Ctrl+Enter"]
        enabled: root.visible
        onActivated: root.submit()
    }

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
        if (key === "frequency" && (value === "none" || value === "preserve")) {
            recurrence.end = "never";
            recurrence.count = 0;
            recurrence.until = "";
        }
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
        next.scope = root.seriesOnly ? "series" : value;
        if (root.recurring)
            next.recurrence = value === "series"
                ? { frequency: "preserve", weekdays: [], end: "never" }
                : { frequency: root.mode === "update" ? "preserve" : "none", weekdays: [], end: "never" };
        root.draftUpdated(next);
    }
    function resetInteraction() {
        root.controlIndex = 0;
        root.weekdayIndex = 0;
        root.confirmDelete = false;
        editorScroll.contentY = 0;
    }
    function scrollToBottom() {
        Qt.callLater(function () {
            editorScroll.contentY = Math.max(0, editorScroll.contentHeight - editorScroll.height);
        });
    }
    function statusCalloutVisible() {
        return root.offline || root.unsupportedEvent || root.noDestination || root.errorText !== "";
    }
    function statusNeedsReveal() {
        return root.offline || root.unsupportedEvent || root.noDestination || root.errorText !== "";
    }
    function revealStatusCallout() {
        if (root.statusNeedsReveal())
            root.scrollToBottom();
    }
    function revealDeleteConfirmation() {
        root.confirmDelete = true;
        root.scrollToBottom();
    }

    onOfflineChanged: revealStatusCallout()
    onUnsupportedEventChanged: revealStatusCallout()
    onNoDestinationChanged: revealStatusCallout()
    onErrorTextChanged: revealStatusCallout()
    function calendarIndex() {
        for (var i = 0; i < destinations.length; i++)
            if (destinations[i].key === root.draft.calendar_key)
                return i;
        return 0;
    }
    function calendarLabel() {
        if (!root.destinations.length)
            return "No owned writable calendar";
        var calendar = root.destinations[root.calendarIndex()];
        var provider = calendar.provider === "google" ? "Google" : "Outlook";
        return String(calendar.name || "Calendar") + "  ·  " + String(calendar.account_label || provider) + "  ·  " + provider;
    }
    function cycleCalendar(amount) {
        if (!destinations.length)
            return;
        var index = (calendarIndex() + Number(amount) + destinations.length) % destinations.length;
        var next = EventEditorModel.withCalendar(
            root.draft, destinations[index].key, root.eventData, root.calendars
        );
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
        root.draftUpdated(EventEditorModel.withRecurrenceEnding(
            root.draft,
            values[(current + Number(amount) + values.length) % values.length]
        ));
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
        var controls = EventEditorModel.visibleControls(root.draft, root.mode, root.recurring, root.hasMeeting, root.canDelete);
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
        else if (controlIndex === 13)
            root.setScope(root.draft.scope === "series" ? "single" : "series");
        else if (controlIndex === 14 && root.confirmDelete && root.recurring)
            root.setScope(root.draft.scope === "series" ? "single" : "series");
    }
    function toggleAllDay() {
        root.draftUpdated(EventEditorModel.toggleAllDay(root.draft));
    }
    function submit() {
        if (!root.canPrimaryAction)
            return;
        if (root.unsupportedEvent) {
            root.duplicateRequested();
            return;
        }
        root.saveRequested();
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
            root.copyMeetingRequested();
        else if (controlIndex === 13)
            root.setScope(root.draft.scope === "series" ? "single" : "series");
        else if (controlIndex === 14) {
            if (root.confirmDelete)
                root.deleteRequested(root.recurring ? root.draft.scope : "single");
            else
                root.revealDeleteConfirmation();
        }
        else if (controlIndex === 15)
            cancelRequested();
        else if (controlIndex === 16)
            submit();
    }
    function handleInputKey(event) {
        if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) {
            root.inputNavigationRequested(event.key === Qt.Key_Backtab || (event.modifiers & Qt.ShiftModifier) ? -1 : 1);
            event.accepted = true;
        } else if (event.key === Qt.Key_Escape) {
            root.confirmDelete ? root.confirmDelete = false : root.cancelRequested();
            event.accepted = true;
        }
    }

    component FieldLabel: Text {
        textFormat: Text.PlainText
        property bool firstSection: false
        topPadding: firstSection ? 0 : Style.space(8)
        color: root.palette.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption * root.textScale
        font.bold: true
    }

    component TimeStepButton: Rectangle {
        property string buttonText: ""
        property int targetControl: 0
        signal activated

        width: Style.space(28)
        height: Style.space(38)
        radius: Style.space(5)
        color: "transparent"
        border.color: root.palette.border
        opacity: root.draft.all_day ? 0.45 : 1
        Text {
            textFormat: Text.PlainText
            anchors.centerIn: parent
            text: parent.buttonText
            color: root.palette.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.body * root.textScale
        }
        MouseArea {
            anchors.fill: parent
            enabled: !root.draft.all_day
            onClicked: {
                root.controlIndex = parent.targetControl;
                parent.activated();
            }
        }
    }

    Text {
        id: editorTitle
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: Style.space(18)
        anchors.topMargin: Style.space(14)
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
        anchors.topMargin: Style.space(14)
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
        objectName: "editorScroll"
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: editorTitle.bottom
        anchors.bottom: footer.top
        anchors.margins: Style.space(16)
        anchors.topMargin: Style.space(14)
        anchors.bottomMargin: Style.space(14)
        contentHeight: form.implicitHeight + Style.space(14)
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        onContentHeightChanged: if (root.confirmDelete || root.statusNeedsReveal())
            root.scrollToBottom()

        Column {
            id: form
            width: editorScroll.width
            spacing: Style.space(6)

            FieldLabel {
                firstSection: true
                text: "Title"
                color: root.controlIndex === 0 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(38)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 0 ? root.palette.accent : root.palette.border
                TextInput {
                    id: titleInput
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    clip: true
                    text: String(root.draft.title || "")
                    autoScroll: activeFocus
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    selectByMouse: true
                    maximumLength: 500
                    onTextEdited: root.update("title", text)
                    onActiveFocusChanged: if (activeFocus) root.controlIndex = 0
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
                height: Style.space(38)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 1 ? root.palette.accent : root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    verticalAlignment: Text.AlignVCenter
                    text: root.calendarLabel()
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
                height: Style.space(38)
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
                        clip: true
                        text: String(root.draft.day || "")
                        horizontalAlignment: Text.AlignHCenter
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall * root.textScale
                        inputMask: "9999-99-99"
                        onTextEdited: root.draftUpdated(EventEditorModel.withDay(root.draft, text))
                        onActiveFocusChanged: if (activeFocus) root.controlIndex = 2
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
                    spacing: Style.space(6)
                    FieldLabel {
                        text: "Start"
                        color: root.controlIndex === 3 ? root.palette.accent : root.palette.foreground
                    }
                    Row {
                        width: parent.width
                        height: Style.space(38)
                        spacing: Style.space(4)
                        Rectangle {
                            width: parent.width - Style.space(64)
                            height: parent.height
                            radius: Style.space(6)
                            color: "transparent"
                            border.color: root.controlIndex === 3 ? root.palette.accent : root.palette.border
                            opacity: root.draft.all_day ? 0.45 : 1
                            TextInput {
                                id: startInput
                                enabled: !root.draft.all_day
                                visible: !root.draft.all_day
                                anchors.fill: parent
                                anchors.margins: Style.space(8)
                                clip: true
                                text: String(root.draft.start || "")
                                horizontalAlignment: Text.AlignHCenter
                                color: root.palette.foreground
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.bodySmall * root.textScale
                                inputMask: "99:99"
                                onTextEdited: root.draftUpdated(EventEditorModel.withTime(root.draft, "start", text))
                                onActiveFocusChanged: if (activeFocus) root.controlIndex = 3
                                Keys.onPressed: function (event) {
                                    root.handleInputKey(event);
                                }
                            }
                        }
                        TimeStepButton {
                            objectName: "startMinus"
                            buttonText: "−"
                            targetControl: 3
                            onActivated: root.draftUpdated(EventEditorModel.shift(root.draft, 0, -15, 0))
                        }
                        TimeStepButton {
                            objectName: "startPlus"
                            buttonText: "+"
                            targetControl: 3
                            onActivated: root.draftUpdated(EventEditorModel.shift(root.draft, 0, 15, 0))
                        }
                    }
                }
                Column {
                    width: (parent.width - parent.spacing) / 2
                    spacing: Style.space(6)
                    FieldLabel {
                        text: "End"
                        color: root.controlIndex === 4 ? root.palette.accent : root.palette.foreground
                    }
                    Row {
                        width: parent.width
                        height: Style.space(38)
                        spacing: Style.space(4)
                        Rectangle {
                            width: parent.width - Style.space(64)
                            height: parent.height
                            radius: Style.space(6)
                            color: "transparent"
                            border.color: root.controlIndex === 4 ? root.palette.accent : root.palette.border
                            opacity: root.draft.all_day ? 0.45 : 1
                            TextInput {
                                id: endInput
                                enabled: !root.draft.all_day
                                visible: !root.draft.all_day
                                anchors.fill: parent
                                anchors.margins: Style.space(8)
                                clip: true
                                text: String(root.draft.end || "")
                                horizontalAlignment: Text.AlignHCenter
                                color: root.palette.foreground
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.bodySmall * root.textScale
                                inputMask: "99:99"
                                onTextEdited: root.draftUpdated(EventEditorModel.withTime(root.draft, "end", text))
                                onActiveFocusChanged: if (activeFocus) root.controlIndex = 4
                                Keys.onPressed: function (event) {
                                    root.handleInputKey(event);
                                }
                            }
                        }
                        TimeStepButton {
                            objectName: "endMinus"
                            buttonText: "−"
                            targetControl: 4
                            onActivated: root.draftUpdated(EventEditorModel.shift(root.draft, 0, 0, -15))
                        }
                        TimeStepButton {
                            objectName: "endPlus"
                            buttonText: "+"
                            targetControl: 4
                            onActivated: root.draftUpdated(EventEditorModel.shift(root.draft, 0, 0, 15))
                        }
                    }
                }
            }

            Item {
                width: 1
                height: Style.space(2)
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
                height: Style.space(38)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 6 ? root.palette.accent : root.palette.border
                TextInput {
                    id: locationInput
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    clip: true
                    text: String(root.draft.location || "")
                    autoScroll: activeFocus
                    color: root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall * root.textScale
                    maximumLength: 500
                    onTextEdited: root.update("location", text)
                    onActiveFocusChanged: if (activeFocus) root.controlIndex = 6
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
                height: Style.space(80)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 7 ? root.palette.accent : root.palette.border
                Flickable {
                    id: notesScroll
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    clip: true
                    contentWidth: width
                    contentHeight: Math.max(height, notesInput.contentHeight)
                    boundsBehavior: Flickable.StopAtBounds
                    TextEdit {
                        id: notesInput
                        width: notesScroll.width
                        height: Math.max(notesScroll.height, contentHeight)
                        text: String(root.draft.notes || "")
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.bodySmall * root.textScale
                        wrapMode: TextEdit.Wrap
                        selectByMouse: true
                        onTextChanged: if (activeFocus)
                            root.update("notes", text)
                        onActiveFocusChanged: if (activeFocus) root.controlIndex = 7
                        Keys.onPressed: function (event) {
                            root.handleInputKey(event);
                        }
                    }
                }
            }

            FieldLabel {
                text: "Recurrence"
                color: root.controlIndex === 8 ? root.palette.accent : root.palette.foreground
            }
            Rectangle {
                width: parent.width
                height: Style.space(38)
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
                height: visible ? Style.space(38) : 0
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
                        clip: true
                        text: String((root.draft.recurrence || {}).count || 1)
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption * root.textScale
                        validator: IntValidator {
                            bottom: 1
                            top: 999
                        }
                        onTextEdited: root.updateRecurrence("count", Number(text))
                        onActiveFocusChanged: if (activeFocus) root.controlIndex = 10
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
                        clip: true
                        text: String((root.draft.recurrence || {}).until || root.draft.day || "")
                        color: root.palette.foreground
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption * root.textScale
                        inputMask: "9999-99-99"
                        onTextEdited: root.updateRecurrence("until", text)
                        onActiveFocusChanged: if (activeFocus) root.controlIndex = 10
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
                height: Style.space(38)
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

            Item {
                visible: root.recurring || root.canDelete
                width: 1
                height: visible ? Style.space(2) : 0
            }
            Rectangle {
                visible: root.recurring && !root.seriesOnly
                width: parent.width
                height: visible ? Style.space(38) : 0
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 13 ? root.palette.accent : root.palette.border
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
                visible: root.canDelete
                width: parent.width
                height: visible ? Style.space(38) : 0
                radius: Style.space(6)
                color: "transparent"
                border.color: root.controlIndex === 14 ? root.palette.foreground : root.palette.urgent
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
                        root.controlIndex = 14;
                        root.revealDeleteConfirmation();
                    }
                }
            }
            Row {
                visible: root.confirmDelete && root.canDelete
                width: parent.width
                height: visible ? Style.space(38) : 0
                spacing: Style.space(6)
                Rectangle {
                    width: root.recurring && !root.seriesOnly ? (parent.width - parent.spacing) / 2 : parent.width
                    height: parent.height
                    radius: Style.space(6)
                    color: root.palette.urgent
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: root.recurring && !root.seriesOnly ? "This occurrence" : root.seriesOnly ? "Delete entire series" : "Confirm delete"
                        color: root.palette.background
                        font.family: root.fontFamily
                        font.pixelSize: Math.max(9, Style.font.caption * root.textScale)
                        font.bold: true
                    }
                    MouseArea {
                        anchors.fill: parent
                        onClicked: root.deleteRequested(root.seriesOnly ? "series" : "single")
                    }
                }
                Rectangle {
                    visible: root.recurring && !root.seriesOnly
                    width: visible ? (parent.width - parent.spacing) / 2 : 0
                    height: parent.height
                    radius: Style.space(6)
                    color: "transparent"
                    border.color: root.palette.urgent
                    Text {
                        textFormat: Text.PlainText
                        anchors.centerIn: parent
                        text: "Entire series"
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

            Item {
                visible: root.statusCalloutVisible()
                width: 1
                height: visible ? Style.space(2) : 0
            }
            Rectangle {
                visible: root.offline || root.unsupportedEvent || root.noDestination || root.errorText !== ""
                width: parent.width
                height: visible ? permissionText.implicitHeight + Style.space(26) : 0
                radius: Style.space(6)
                color: Qt.rgba(0.97, 0.46, 0.56, 0.08)
                border.color: root.offline || root.errorText !== "" ? root.palette.urgent : root.palette.accent
                Text {
                    id: permissionText
                    textFormat: Text.PlainText
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: Style.space(12)
                    text: root.offline ? "Editing is unavailable offline. Your draft is preserved." : root.errorText !== "" ? root.errorText : root.unsupportedEvent ? root.unsupportedReason : "No owned writable calendar is available."
                    color: root.offline || root.errorText !== "" ? root.palette.urgent : root.palette.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    wrapMode: Text.Wrap
                }
            }
            Item {
                width: 1
                height: Style.space(2)
            }
            Rectangle {
                objectName: "editorKeyboardHints"
                width: parent.width
                height: Style.space(82)
                radius: Style.space(6)
                color: "transparent"
                border.color: root.palette.border
                Text {
                    textFormat: Text.PlainText
                    anchors.fill: parent
                    anchors.margins: Style.space(10)
                    text: "h / l  Change value\nj / k  Change field\nCtrl+Enter  Save\nEsc  Cancel"
                    color: root.palette.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption * root.textScale
                    lineHeight: 1.25
                }
            }
        }
    }

    Rectangle {
        objectName: "editorScrollAffordance"
        visible: editorScroll.contentHeight > editorScroll.height
        anchors.top: editorScroll.top
        anchors.right: parent.right
        anchors.rightMargin: Style.space(6)
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
        anchors.bottomMargin: Style.space(14)
        height: Style.space(38)
        spacing: Style.space(8)
        Rectangle {
            width: (parent.width - parent.spacing) * 0.4
            height: parent.height
            radius: Style.space(6)
            color: "transparent"
            border.color: root.controlIndex === 15 ? root.palette.accent : root.palette.border
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
            border.color: root.controlIndex === 16 ? root.palette.foreground : root.palette.accent
            color: root.canPrimaryAction ? root.palette.accent : root.palette.border
            Text {
                textFormat: Text.PlainText
                anchors.centerIn: parent
                text: root.busy ? "Saving" : root.unsupportedEvent ? "Duplicate" : "Save  Ctrl+Enter"
                color: root.canPrimaryAction ? root.palette.background : root.palette.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption * root.textScale
                font.bold: true
            }
            MouseArea {
                anchors.fill: parent
                enabled: root.canPrimaryAction
                onClicked: root.submit()
            }
        }
    }
}
