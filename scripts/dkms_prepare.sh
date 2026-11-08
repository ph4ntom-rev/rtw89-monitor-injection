#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
#
# dkms_prepare.sh
# Copy rtw89 sources of a given kernel series into /usr/src and register them
# with DKMS. DKMS then rebuilds the patched modules only for kernels of the
# same series (BUILD_EXCLUSIVE_KERNEL); other kernels keep the stock driver.
#
# Usage:
#   scripts/dkms_prepare.sh --source /usr/src/linux-source-7.3.tar.xz
#   scripts/dkms_prepare.sh --source /path/to/linux-tree
#   scripts/dkms_prepare.sh --remove

set -euo pipefail

PKG="rtw89-monitor-injection"
VER="5.0"
DEST="${RTW89_DKMS_DEST:-/usr/src/${PKG}-${VER}}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KCONFIG="${RTW89_KCONFIG:-/lib/modules/$(uname -r)/build/.config}"

die() { echo "[ERROR] $*" >&2; exit 1; }

usage() {
	sed -n '4,13p' "$0" | sed 's/^# \{0,1\}//'
}

SRC=""
REMOVE=0
while [[ $# -gt 0 ]]; do
	case "$1" in
	--source) SRC="${2:?--source needs an argument}"; shift 2 ;;
	--remove) REMOVE=1; shift ;;
	-h|--help) usage; exit 0 ;;
	*) usage; die "unknown argument: $1" ;;
	esac
done

command -v dkms >/dev/null || die "dkms is not installed"

if [[ ${REMOVE} -eq 1 ]]; then
	sudo dkms remove "${PKG}/${VER}" --all || true
	sudo rm -rf "${DEST}"
	echo "[OK] Removed ${PKG}/${VER}"
	exit 0
fi

[[ -n "${SRC}" ]] || { usage; die "--source is required"; }
[[ -f "${KCONFIG}" ]] || die "missing ${KCONFIG}; install the kernel headers"

WORK="$(mktemp -d)"
trap 'rm -rf "${WORK}"' EXIT

if [[ -d "${SRC}" ]]; then
	TREE="$(realpath "${SRC}")"
else
	# head closes the pipe early; do not let SIGPIPE in tar abort the script
	top="$(set +o pipefail; tar -tf "${SRC}" | head -n 1 | cut -d/ -f1)"
	tar -xf "${SRC}" -C "${WORK}" "${top}/Makefile" \
		--wildcards "${top}/drivers/net/wireless/realtek/rtw89/*"
	TREE="${WORK}/${top}"
fi

RTW="${TREE}/drivers/net/wireless/realtek/rtw89"
[[ -f "${RTW}/core.c" && -f "${RTW}/Makefile" ]] || die "no rtw89 sources in ${SRC}"

series="$(awk '/^VERSION =/ {v=$3} /^PATCHLEVEL =/ {p=$3} END {print v "." p}' "${TREE}/Makefile")"
[[ "${series}" =~ ^[0-9]+\.[0-9]+$ ]] || die "could not read the kernel version from ${TREE}/Makefile"

running="$(uname -r | cut -d. -f1,2)"
if [[ "${series}" != "${running}" ]]; then
	echo "[WARN] rtw89 sources are from Linux ${series}, running kernel is $(uname -r)."
	echo "       DKMS will only build for ${series}.x kernels."
fi

# Modules to install: every rtw89 module in the source Makefile that is
# enabled as a module in the kernel configuration.
modules=()
while read -r sym mod; do
	if grep -q "^${sym}=m" "${KCONFIG}"; then
		modules+=("${mod}")
	fi
done < <(sed -nE 's/^obj-\$\((CONFIG_[A-Z0-9_]+)\)[[:space:]]*\+=[[:space:]]*(rtw89_[a-z0-9_]+)\.o.*/\1 \2/p' "${RTW}/Makefile")

[[ ${#modules[@]} -gt 0 ]] || die "no rtw89 modules enabled in ${KCONFIG}"

module_lines=""
for i in "${!modules[@]}"; do
	module_lines+="BUILT_MODULE_NAME[${i}]=\"${modules[$i]}\""$'\n'
	module_lines+="DEST_MODULE_LOCATION[${i}]=\"/updates/rtw89\""$'\n'
done

regex="^${series//./\\.}\\."

conf="$(python3 - "${REPO_DIR}/dkms/dkms.conf.in" "${PKG}" "${VER}" "${regex}" "${module_lines}" <<'EOF'
import sys

template, pkg, ver, regex, modules = sys.argv[1:6]
text = open(template, encoding="utf-8").read()
for key, value in (("@PKG@", pkg), ("@VER@", ver),
                   ("@KVER_REGEX@", regex), ("@MODULES@", modules)):
    text = text.replace(key, value)
sys.stdout.write(text)
EOF
)"

sudo dkms remove "${PKG}/${VER}" --all >/dev/null 2>&1 || true
sudo rm -rf "${DEST}"
sudo mkdir -p "${DEST}/src" "${DEST}/scripts"
sudo cp -a "${RTW}/." "${DEST}/"
sudo cp "${REPO_DIR}/src/rtw89_mon.c" "${REPO_DIR}/src/rtw89_mon.h" "${DEST}/src/"
sudo cp "${REPO_DIR}/scripts/patch_rtw89.py" "${REPO_DIR}/scripts/dkms_pre_build.sh" "${DEST}/scripts/"
sudo chmod +x "${DEST}/scripts/dkms_pre_build.sh"
echo "${series}" | sudo tee "${DEST}/RTW89_SOURCE_KERNEL" >/dev/null
printf '%s\n' "${conf}" | sudo tee "${DEST}/dkms.conf" >/dev/null

sudo dkms add "${PKG}/${VER}"
echo "[OK] Registered ${PKG}/${VER} for Linux ${series}.x with modules: ${modules[*]}"
echo "     Build and install for the running kernel with:"
echo "       sudo dkms install ${PKG}/${VER}"
