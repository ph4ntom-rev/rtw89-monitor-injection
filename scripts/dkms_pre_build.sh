#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
#
# DKMS PRE_BUILD hook. Refuses to build when the copied rtw89 sources come
# from a different kernel series than the target kernel, then applies the
# monitor/injection integration to the DKMS build directory.

set -eu

kver="${1:?usage: dkms_pre_build.sh <kernelver>}"
src_series="$(cat RTW89_SOURCE_KERNEL)"
target_series="$(echo "$kver" | cut -d. -f1,2)"

if [ "$src_series" != "$target_series" ]; then
	echo "rtw89-monitor-injection: rtw89 sources are from Linux $src_series," >&2
	echo "but the target kernel is $kver. Re-run scripts/dkms_prepare.sh" >&2
	echo "with the linux-source package of the new kernel." >&2
	exit 1
fi

python3 scripts/patch_rtw89.py --rtw89-dir . --apply
