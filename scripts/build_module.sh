#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
#
# build_module.sh
# Linux Kbuild helper for patching, compiling, reloading, installing, or
# uninstalling the rtw89 monitor-mode driver fixes on Linux 6.1 - 7.x+.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KVER="$(uname -r)"
KBUILD_DIR="/lib/modules/${KVER}/build"
UPDATES_DIR="/lib/modules/${KVER}/updates/rtw89"
MODPROBE_CONF="/etc/modprobe.d/rtw89-monitor.conf"

RTW89_DIR=""
DO_CHECK_ONLY=0
DO_RELOAD=0
DO_INSTALL=0
DO_UNINSTALL=0
DO_DISABLE_PS=0
LOADED_LEAF_MODS=()

usage() {
    cat <<EOF
Usage:
  $0 --check-env
  $0 --rtw89-dir /path/to/rtw89 [--reload] [--install] [--disable-ps]
  $0 --uninstall

Options:
  --check-env        Inspect kernel version, headers, rtw89 PCIe/USB state, and dmesg
  --rtw89-dir <dir>  Patch <dir> and compile rtw89 kernel modules via Kbuild
  --reload           Unload currently loaded rtw89 modules and load the newly built modules
  --install          Install built .ko files into ${UPDATES_DIR} and run depmod -a
  --disable-ps       Optional: write disable_ps_mode=y to ${MODPROBE_CONF}
  --uninstall        Remove ${UPDATES_DIR} override and restore stock distro rtw89 modules
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check-env)
            DO_CHECK_ONLY=1
            shift
            ;;
        --rtw89-dir)
            RTW89_DIR="$(realpath "$2")"
            shift 2
            ;;
        --reload)
            DO_RELOAD=1
            shift
            ;;
        --install)
            DO_INSTALL=1
            shift
            ;;
        --disable-ps)
            DO_DISABLE_PS=1
            shift
            ;;
        --uninstall)
            DO_UNINSTALL=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

ALL_LEAF_CANDIDATES=(
    rtw89_8852ae rtw89_8852be rtw89_8852bte rtw89_8852ce rtw89_8851be rtw89_8922ae
    rtw89_8852au rtw89_8852bu rtw89_8851bu
    rtw_8852ae rtw_8852be rtw_8852ce rtw_8851be
)

detect_loaded_leaf_modules() {
    LOADED_LEAF_MODS=()
    local mod
    for mod in "${ALL_LEAF_CANDIDATES[@]}"; do
        if lsmod | awk '{print $1}' | grep -qx "${mod}"; then
            LOADED_LEAF_MODS+=("${mod}")
        fi
    done
    if [[ ${#LOADED_LEAF_MODS[@]} -eq 0 ]]; then
        LOADED_LEAF_MODS=(rtw89_8852ae)
    fi
}

unload_rtw89_stack() {
    local mod
    for mod in \
        rtw89_8852ae rtw89_8852be rtw89_8852bte rtw89_8852ce rtw89_8851be rtw89_8922ae \
        rtw89_8852au rtw89_8852bu rtw89_8851bu \
        rtw89_8852a rtw89_8852b rtw89_8852bt rtw89_8852b_common rtw89_8852c rtw89_8851b rtw89_8922a \
        rtw89_pci rtw89_usb rtw89_core \
        rtw_8852ae rtw_8852a rtw89pci rtw89core; do
        if lsmod | awk '{print $1}' | grep -qx "${mod}"; then
            sudo rmmod "${mod}" && echo "  unloaded ${mod}"
        fi
    done
}

if [[ ${DO_UNINSTALL} -eq 1 ]]; then
    echo "=== Uninstalling rtw89 updates override for kernel ${KVER} ==="
    detect_loaded_leaf_modules
    sudo rm -rf "${UPDATES_DIR}"
    sudo rm -f "${MODPROBE_CONF}"
    sudo depmod -a "${KVER}"
    unload_rtw89_stack
    for mod in "${LOADED_LEAF_MODS[@]}"; do
        sudo modprobe "${mod}" || true
    done
    echo "[OK] Restored distribution rtw89 modules (${LOADED_LEAF_MODS[*]})."
    exit 0
fi

echo "=== [1] Linux Kernel & rtw89 Environment Check ==="
echo "  Kernel release (uname -r) : ${KVER}"
if [[ -d "${KBUILD_DIR}" ]]; then
    echo "  Kernel build headers      : FOUND (${KBUILD_DIR})"
else
    echo "  Kernel build headers      : MISSING (${KBUILD_DIR})"
    echo "    -> Debian/Ubuntu/Kali : sudo apt install linux-headers-\$(uname -r) build-essential"
    echo "    -> Fedora/RHEL        : sudo dnf install kernel-devel-\$(uname -r) gcc make"
    echo "    -> Arch Linux         : sudo pacman -S linux-headers base-devel"
fi

if command -v lspci >/dev/null 2>&1; then
    echo "  PCIe Realtek Wi-Fi check  :"
    lspci -nn | grep -iE "10ec:(8851|8852|8922)" || echo "    (no Realtek rtw89 PCIe device reported by lspci)"
fi

echo "  Recent rtw89 dmesg lines  :"
sudo dmesg 2>/dev/null | grep -iE "rtw89|8852|8851|8922" | tail -n 10 || true

if [[ ${DO_CHECK_ONLY} -eq 1 ]]; then
    cat <<EOF

=== Obtaining matching rtw89 kernel driver sources ===
  Option A (Matching distribution kernel source package - recommended):
    sudo apt install linux-source
    mkdir -p ~/rtw89_build && tar -xf /usr/src/linux-source-*.tar.xz \\
      --strip-components=6 -C ~/rtw89_build \\
      --wildcards '*/drivers/net/wireless/realtek/rtw89/*'
    $0 --rtw89-dir ~/rtw89_build --install

  Option B (Upstream wireless-next / mainline git tree):
    git am patches/v4/v4-000[1-4]*.patch
EOF
    exit 0
fi

if [[ -z "${RTW89_DIR}" ]]; then
    echo "[ERROR] --rtw89-dir is required unless --check-env or --uninstall is used." >&2
    usage
    exit 1
fi

if [[ ! -d "${KBUILD_DIR}" ]]; then
    echo "[ERROR] Kernel headers directory ${KBUILD_DIR} is missing." >&2
    exit 1
fi

echo ""
echo "=== [2] Applying rtw89 Monitor & Raw TX Integration ==="
python3 "${SCRIPT_DIR}/patch_rtw89.py" --rtw89-dir "${RTW89_DIR}" --apply

echo ""
echo "=== [3] Compiling rtw89 Kernel Modules ==="
make -C "${KBUILD_DIR}" M="${RTW89_DIR}" -j"$(nproc)" modules

echo ""
echo "Built modules in ${RTW89_DIR}:"
ls -lh "${RTW89_DIR}"/*.ko

if [[ ${DO_INSTALL} -eq 1 ]]; then
    echo ""
    echo "=== [4] Installing Modules into ${UPDATES_DIR} ==="
    detect_loaded_leaf_modules
    sudo mkdir -p "${UPDATES_DIR}"
    sudo cp -f "${RTW89_DIR}"/*.ko "${UPDATES_DIR}/"
    if [[ ${DO_DISABLE_PS} -eq 1 ]]; then
        printf "options rtw89_core disable_ps_mode=y\n" | sudo tee "${MODPROBE_CONF}" >/dev/null
    fi
    sudo depmod -a "${KVER}"
    unload_rtw89_stack
    for mod in "${LOADED_LEAF_MODS[@]}"; do
        sudo modprobe "${mod}"
    done
    echo "[OK] Installed and loaded: ${LOADED_LEAF_MODS[*]}"
elif [[ ${DO_RELOAD} -eq 1 ]]; then
    echo ""
    echo "=== [4] Reloading rtw89 Modules (live session) ==="
    detect_loaded_leaf_modules
    unload_rtw89_stack

    PS_OPT=""
    [[ ${DO_DISABLE_PS} -eq 1 ]] && PS_OPT="disable_ps_mode=y"

    if [[ -f "${RTW89_DIR}/rtw89_core.ko" ]]; then
        sudo insmod "${RTW89_DIR}/rtw89_core.ko" ${PS_OPT}
        [[ -f "${RTW89_DIR}/rtw89_pci.ko" ]] && sudo insmod "${RTW89_DIR}/rtw89_pci.ko"
        [[ -f "${RTW89_DIR}/rtw89_usb.ko" ]] && sudo insmod "${RTW89_DIR}/rtw89_usb.ko"
        [[ -f "${RTW89_DIR}/rtw89_8852b_common.ko" ]] && sudo insmod "${RTW89_DIR}/rtw89_8852b_common.ko"
        for base in rtw89_8852a rtw89_8852b rtw89_8852bt rtw89_8852c rtw89_8851b rtw89_8922a; do
            [[ -f "${RTW89_DIR}/${base}.ko" ]] && sudo insmod "${RTW89_DIR}/${base}.ko" || true
        done
        for mod in "${LOADED_LEAF_MODS[@]}"; do
            [[ -f "${RTW89_DIR}/${mod}.ko" ]] && sudo insmod "${RTW89_DIR}/${mod}.ko"
        done
    else
        echo "[ERROR] Could not locate built rtw89_core.ko in ${RTW89_DIR}" >&2
        exit 1
    fi
fi

if [[ ${DO_INSTALL} -eq 1 || ${DO_RELOAD} -eq 1 ]]; then
    echo ""
    echo "=== [5] Post-Load Kernel Log Check ==="
    sleep 1
    sudo dmesg | grep -iE "rtw89" | tail -n 15 || true
fi
