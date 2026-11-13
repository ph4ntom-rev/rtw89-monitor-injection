#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only OR BSD-3-Clause
#
# prepare_submission.sh
#
# Read-only preflight validator for the upstream rtw89 PATCH series in
# patches/v5/. Verifies cover-letter completeness, patch count, author /
# Signed-off-by / Fixes / stable tags, absence of out-of-tree markers or CRLF
# line endings, and (when --kernel-dir is supplied) checkpatch.pl --strict.
#
# Safety guarantee: this script NEVER sends email and never invokes
# `git send-email` without `--dry-run` (it only prints the future dry-run
# command for manual operator inspection).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERIES_DIR="${REPO_ROOT}/patches/v5"
KERNEL_DIR=""

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --series-dir <path>   Patch series directory (default: patches/v5)
  --kernel-dir <path>   Optional Linux kernel source tree for checkpatch.pl / get_maintainer.pl
  -h, --help            Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --series-dir)
            [[ $# -ge 2 ]] || { echo "[ERROR] --series-dir requires a path" >&2; exit 1; }
            SERIES_DIR="$2"
            shift 2
            ;;
        --kernel-dir)
            [[ $# -ge 2 ]] || { echo "[ERROR] --kernel-dir requires a path" >&2; exit 1; }
            KERNEL_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

echo "=== rtw89 Upstream Submission Preflight (Read-Only, No Mail Sent) ==="
echo "  Series directory : ${SERIES_DIR}"

cover="${SERIES_DIR}/v5-0000-cover-letter.patch"
if [[ ! -f "${cover}" ]]; then
    echo "[FAIL] Missing cover letter: ${cover}" >&2
    exit 1
fi

if grep -qE '\*\*\* SUBJECT HERE \*\*\*|\*\*\* BLURB HERE \*\*\*' "${cover}"; then
    echo "[FAIL] Cover letter still contains template placeholders" >&2
    exit 1
fi

if ! grep -q '^base-commit: [0-9a-f]\{40\}$' "${cover}"; then
    echo "[FAIL] Cover letter is missing a 40-char base-commit footer" >&2
    exit 1
fi

shopt -s nullglob
patches=("${SERIES_DIR}"/v5-00*-wifi-*.patch)
shopt -u nullglob

count="${#patches[@]}"
if [[ "${count}" -ne 10 ]]; then
    echo "[FAIL] Expected 10 numbered patches in ${SERIES_DIR}, found ${count}" >&2
    exit 1
fi
echo "[PASS] Found cover letter + ${count} numbered patches"

idx=0
for p in "${cover}" "${patches[@]}"; do
    bname="$(basename "${p}")"
    if LC_ALL=C grep -q $'\r' "${p}"; then
        echo "[FAIL] CRLF line endings detected in ${bname}" >&2
        exit 1
    fi
    if grep -q 'RTW89_MON_EXT' "${p}"; then
        echo "[FAIL] Out-of-tree marker RTW89_MON_EXT leaked into ${bname}" >&2
        exit 1
    fi
    if ! grep -q '^From: Enver Ikromov <owner@ph4ntom-rev.uz>$' "${p}"; then
        echo "[FAIL] Missing canonical From: header in ${bname}" >&2
        exit 1
    fi
    if [[ "${idx}" -gt 0 ]]; then
        if ! grep -q '^Signed-off-by: Enver Ikromov <owner@ph4ntom-rev.uz>$' "${p}"; then
            echo "[FAIL] Missing Signed-off-by trailer in ${bname}" >&2
            exit 1
        fi
    fi
    if [[ "${idx}" -eq 1 || "${idx}" -eq 2 ]]; then
        if ! grep -q '^Fixes: [0-9a-f]\{12\} ("wifi: rtw89:' "${p}" && \
           ! grep -q '^Fixes: [0-9a-f]\{12\} ("rtw89:' "${p}"; then
            echo "[FAIL] Bugfix patch ${bname} is missing a canonical Fixes: tag" >&2
            exit 1
        fi
        if ! grep -q '^Cc: stable@vger.kernel.org$' "${p}"; then
            echo "[FAIL] Bugfix patch ${bname} is missing Cc: stable@vger.kernel.org" >&2
            exit 1
        fi
    fi
    idx=$((idx + 1))
done
echo "[PASS] Metadata, trailers, Fixes/stable tags, and LF line endings verified"

if [[ -n "${KERNEL_DIR}" ]]; then
    checkpatch="${KERNEL_DIR}/scripts/checkpatch.pl"
    if [[ ! -f "${checkpatch}" ]]; then
        echo "[FAIL] checkpatch.pl not found at ${checkpatch}" >&2
        exit 1
    fi
    echo "[INFO] Running checkpatch.pl --strict against ${count} patches..."
    perl "${checkpatch}" --strict --no-tree --ignore UNKNOWN_COMMIT_ID "${patches[@]}"
    echo "[PASS] checkpatch.pl --strict passed with 0 errors / 0 warnings / 0 checks"

    if [[ -f "${KERNEL_DIR}/scripts/get_maintainer.pl" && \
          -f "${KERNEL_DIR}/MAINTAINERS" && \
          -d "${KERNEL_DIR}/arch" && \
          -d "${KERNEL_DIR}/include" ]]; then
        echo "[INFO] Upstream recipients from get_maintainer.pl (informational only):"
        (
            cd "${KERNEL_DIR}"
            perl ./scripts/get_maintainer.pl --nogit --nogit-fallback --norolestats "${patches[@]}" | sed 's/^/         /' || true
        )
    fi
fi

cat <<EOF

[READY] Series validated locally. No email was sent.
When ready for manual dry-run inspection in the future, run:
  git send-email --dry-run \\
    --to="Ping-Ke Shih <pkshih@realtek.com>" \\
    --cc="linux-wireless@vger.kernel.org" \\
    ${SERIES_DIR}/v5-*.patch
EOF
