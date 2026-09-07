# Flight Deck Calendar installation

Flight Deck Calendar puts Google Calendar and Outlook in one Omarchy panel. Read-only access remains the default. It does not install a separate helper, timer, or service, and it does not change Hyprland automatically.

## Install

```bash
omarchy plugin add https://github.com/joryeugene/omarchy-calendar.git --enable
```

The installed plugin ID is `io.github.joryeugene.omarchy-calendar`. Its bundled helper is:

```bash
calendarctl="$HOME/.config/omarchy/plugins/io.github.joryeugene.omarchy-calendar/calendarctl"
"$calendarctl" status
```

Try the built-in offline dataset without an account:

```bash
"$calendarctl" demo seed
"$calendarctl" status
```

## Optional launch binding

`Super+Shift+C` is optional and the plugin does not claim it during installation. If the chord is free on the machine, add this user-owned binding to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + SHIFT + C", "Flight Deck calendar", "omarchy-shell shell toggle io.github.joryeugene.omarchy-calendar")
```

Reload Hyprland and confirm `hyprctl configerrors` is empty. Remove the single line to roll back the binding.

## Provider setup

Press `c`, choose Google Calendar or Outlook.com, review the requested read-only access, and choose **Connect in browser**. Complete provider consent in the browser, then return to Flight Deck. The provider is connected only after the first successful sync populates the local cache.

### Google Calendar

Google uses PKCE S256 and receives only identity, `calendar.events.readonly`, and `calendar.calendarlist.readonly` access.

### Personal Outlook.com

Microsoft uses the `/consumers` authority and receives identity, profile, offline access, `User.Read`, and `Calendars.Read`. It uses no application credential. The account remains read-only by default.

For either provider, press `c` and choose Connect when browser consent needs to be repeated.

### Advanced provider override

Contributors can replace either bundled public desktop registration without editing source code. A valid local override always takes precedence, and updates do not replace existing tokens or provider settings.

For Google, import the Desktop credentials JSON from a separate Google Cloud project. The import stores only the public client ID in `~/.config/omarchy-calendar/providers.json`. The Desktop app credential goes directly to a distinct Secret Service keyring item and is not written to provider settings, source code, command arguments, or output.

```bash
"$calendarctl" import-google-desktop-app /path/to/google-desktop-credentials.json
"$calendarctl" auth google
```

For Microsoft, configure the public Application client ID from a personal-account capable desktop registration:

```bash
"$calendarctl" configure-client microsoft PUBLIC_MICROSOFT_DESKTOP_ID
"$calendarctl" auth microsoft
```

Provider client-ID overrides are written to `~/.config/omarchy-calendar/providers.json` with mode `0600`. OAuth tokens and an imported Google Desktop app credential are stored in the system keyring. Bundled public registrations remain in the installed plugin. Cached events are stored in `~/.local/state/omarchy-calendar/calendar.db` with mode `0600`.

## Enable optional event editing

Choose **Read and edit** while connecting a new account, or choose **Enable editing** for an existing account in **Accounts and Calendars**. A blocked create, edit, duplicate, or quick-move action opens that exact account action and resumes only after successful consent. Google adds `calendar.events.owned`; Outlook adds `Calendars.ReadWrite`. The read-only scopes remain available so Today, Week, and calendar filtering continue to work.

The terminal provides the same account-specific upgrade:

```bash
"$calendarctl" enable-editing google
"$calendarctl" enable-editing microsoft
```

Editing is limited to calendars owned by the connected account. Only changes confirmed with Save are sent to the provider. Delete requires a second confirmation. Copying an event to another calendar does not delete the original event, and Flight Deck offers Delete original only after the destination copy has been verified.

New events can repeat daily, on weekdays, weekly, monthly, or on selected weekdays. A series can continue indefinitely, stop after a set number of events, or end on a date. Existing recurring events can apply a change to **This occurrence** or the **Entire series**. **This and following** is not supported.

An **Entire series** copy transfers the supported base schedule. Flight Deck checks the source series for modified or cancelled occurrences before offering Delete original. It offers Delete original only when the scan is complete and finds none; otherwise, it keeps the original series and explains why.

When an event already has a meeting link, a copy can keep the existing meeting link or generate a new meeting when the destination calendar reports that capability. Flight Deck verifies the destination event before reporting success. If the provider has not returned a generated link yet, Flight Deck reports that the meeting link is still pending and leaves the original unchanged.

Flight Deck offers Delete original only when the source is eligible for deletion, online, and authorized for event editing. An event is not eligible when it has attendees, the connected user is not its organizer, or its source calendar is shared or read-only. If the source account lacks write permission, the result offers a separate permission upgrade. Granting that permission never deletes the original automatically.

For a cross-provider copy, Flight Deck sends the title, day and time, all-day state, location, notes, supported recurrence, and meeting choice directly from the workstation to the destination provider over HTTPS. No event data passes through a Flight Deck server. The destination event is created and verified before Flight Deck offers any action on the source event. If an existing provider series cannot be represented by the supported recurrence presets, Flight Deck refuses the copy instead of flattening the series into one event.

Events with attendees are duplicate-only in this release. Flight Deck does not edit, delete, or send invitations for them.

Dragging and resizing change only the local draft. The unsaved draft remains available while the provider is offline, and Save remains disabled until the account is online. If a provider revision no longer matches the cached event, Flight Deck cancels the write, refreshes the provider event, and preserves the local draft for review. If a create request is retried after an uncertain response, Flight Deck reuses the same provider request identifier so the retry does not create a second event.

The editor uses `j` and `k` to move between fields and `h` and `l` to change the selected value. `Shift+H` and `Shift+L` move the local draft by one day. `Shift+J` and `Shift+K` move a timed local draft by 15 minutes; all-day events move by day only. Press `Enter` on the recurrence scope to choose **This occurrence** or **Entire series**, and press it on Delete to reveal and confirm the deletion. Press `Ctrl+Enter` to save or `Esc` to cancel. After a copy, press `k` to keep both events, `e` to enable source editing when required, or `x` twice to reveal and confirm Delete original.

## Operations

```bash
"$calendarctl" sync
"$calendarctl" sync --provider google
"$calendarctl" sync --provider microsoft
"$calendarctl" status
"$calendarctl" setup-status
"$calendarctl" disconnect google
"$calendarctl" disconnect microsoft
"$calendarctl" reset-local-data
```

One singleton Omarchy service refreshes every 5, 15, or 30 minutes according to inline settings. No user systemd unit is involved. On network failure, the last successful local cache remains visible and is marked stale.

## Health checks

```bash
omarchy plugin validate "$HOME/.config/omarchy/plugins/io.github.joryeugene.omarchy-calendar"
hyprctl configerrors
omarchy-shell shell ping
"$calendarctl" status
"$calendarctl" setup-status
```

## Uninstall

To remove local calendar data and tokens first:

```bash
"$calendarctl" reset-local-data
```

Then remove the plugin:

```bash
omarchy plugin remove io.github.joryeugene.omarchy-calendar
```

Remove the optional `Super+Shift+C` binding separately if it was added. Provider calendars are never modified.
