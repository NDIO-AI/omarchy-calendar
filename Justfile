# SPDX-License-Identifier: GPL-3.0-or-later

default: check

check:
    @./scripts/check

e2e:
    @./scripts/e2e isolated

e2e-native:
    @./scripts/e2e native

e2e-live:
    @./scripts/e2e live

release:
    @./scripts/e2e release
