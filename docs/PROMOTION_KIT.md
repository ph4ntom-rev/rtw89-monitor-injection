# Community Promotion, Write-ups & Outreach Kit

This document contains pre-written, highly technical write-ups, community comments, and RFC templates ready to copy and post to maximize visibility and adoption of `rtw89-monitor-injection`.

---

## 1. Targeted GitHub Issue Comments

### Target 1: `lwfinger/rtw89` — Issue #45 ("Unable to use airplay deauth") & Issue #336
**URL**: `https://github.com/lwfinger/rtw89/issues/45`

**Comment Text**:
```markdown
Hello everyone,

I've investigated why packet injection / deauth frames (`aireplay-ng`) and raw TX consistently fail or cause kernel panics on RTL8852AE/BE with the stock `rtw89` driver.

By reverse-engineering the RISC-V firmware scheduling state machine and MAC register behaviour in IDA Pro, we found four distinct host-driver root causes rather than a firmware limitation:
1. **Uninitialized MACID Scheduler Context**: The firmware keeps the transmit queue paused for unassociated monitor interfaces unless `h2c_default_cmac_tbl` / `h2c_default_dmac_tbl` are initialized and registered via `rtw89_fw_h2c_join_info()`. Using `RTW89_SELF_ROLE_AP` with `RTW89_NET_TYPE_NO_LINK` initializes the MACID without triggering client beacon-loss timeouts or broadcasting beacons.
2. **Band 0 MAC Promiscuous Filtering (`0xCE20` - `0xCE2C`)**: Hardware registers `R_AX_RX_FLTR_OPT`, `R_AX_CTRL_FLTR`, and `R_AX_DATA_FLTR` drop Control frames (ACK/RTS/CTS) and cross-BSS Data by default unless unlocked (`0x55555555`).
3. **`NULL` `vif` Dereference on Injection**: `mac80211` passes `info->control.vif = NULL` on injected Radiotap frames, which triggered an unconditional `vif_to_rtwvif(vif)` NULL pointer dereference in `rtw89_ops_tx()` and `rtw89_core_tx_write()`.
4. **Invalid Rate Index**: When Radiotap headers omit an explicit rate, `mac80211` sets `rates[0].idx = -1`, which was cast to `(u8)255`, writing an invalid hardware rate and leaving `desc_info->en_wd_info` unset.

We developed an open-source host-side patchset that enables full monitor mode and packet injection on RTL8852AE using 100% stock, unmodified `linux-firmware` images (compatible with Linux 6.1 through 7.x+ and `lwfinger/rtw89`):
👉 https://github.com/ph4ntom-rev/rtw89-monitor-injection

Hope this helps anyone looking to get `airmon-ng` and `aireplay-ng` working on modern Realtek 802.11ax laptops!
```

---

### Target 2: `morrownr/USB-WiFi` & `morrownr/rtw89`
**URL**: `https://github.com/morrownr/USB-WiFi/issues` or `https://github.com/morrownr/rtw89/issues`

**Comment Text**:
```markdown
Hi @morrownr and community,

Thank you for your fantastic work tracking Linux Wi-Fi adapters and driver status!

Regarding the status of Monitor Mode and Packet Injection on Realtek 802.11ax chips (`rtw89` / RTL8852AE / RTL8852BE): we have published a host-driver extension that enables full `NL80211_IFTYPE_MONITOR` packet capture and `IEEE80211_TX_CTL_INJECTED` raw frame transmission without needing any modified firmware binaries.

Repository & Technical Documentation:
https://github.com/ph4ntom-rev/rtw89-monitor-injection

Key architectural highlights:
- Works with official, unmodified `/lib/firmware/rtw89/` binaries.
- Configures Band 0 MAC registers (`R_AX_RX_FLTR_OPT` 0xCE20, `R_AX_CTRL_FLTR` 0xCE24, etc.) to capture all 16 Control and Management subtypes.
- Configures H2C scheduler context (`RTW89_SELF_ROLE_AP` + `RTW89_NET_TYPE_NO_LINK`) and unpauses `mac_id` so raw TX queues transmit frames cleanly.
- Fixes `NULL` `vif` dereferences and `rates[0].idx = -1` descriptor defaults.
- Tested and verified on Kali Linux with `airmon-ng`, `airodump-ng`, and `aireplay-ng -9`.

Feel free to reference or integrate this into the driver documentation.
```

---

## 2. Reddit Posts

### Post A: For `r/ReverseEngineering` & `r/netsec`
**Title**:
```text
Reverse Engineering Realtek RTL8852AE (rtw89) Firmware & MAC Registers: Enabling Monitor Mode and Packet Injection via Host Driver Hooks
```

**Body**:
```markdown
For the past couple of years, Realtek's 802.11ax chips (specifically RTL8852AE, RTL8852BE) have gained a reputation in the Linux and pentesting communities for being completely incapable of raw packet injection (`aireplay-ng`) and having broken/incomplete monitor mode. The standard community consensus was that the closed-source RISC-V firmware blobs locked down TX on monitor interfaces.

After inspecting the mainline `rtw89` driver and reverse-engineering the firmware communication protocol in IDA Pro, we found that the firmware does not block injection out of malice—the host driver was simply leaving the hardware MAC in a filtering state and failing to initialize the MACID context in the firmware's internal scheduler.

### Root Cause Analysis & Hardware Fixes:

1. **Hardware MAC RX Subtype Filtering (`0xCE20` – `0xCE2C`)**:
   Mainline `rtw89` only configures basic RX filters. Band 0 MAC registers `R_AX_RX_FLTR_OPT` (`0xCE20`), `R_AX_CTRL_FLTR` (`0xCE24`), `R_AX_MGNT_FLTR` (`0xCE28`), and `R_AX_DATA_FLTR` (`0xCE2C`) actively drop 802.11 Control frames (ACK, RTS, CTS, BlockAck) and cross-BSS unicast frames because `ADDR_CAM` matching is enforced by default. Programming these registers with `0x55555555` unlocks all 16 subtypes to the host DMA rings.

2. **Firmware Scheduler Pause & The `RTW89_SELF_ROLE_AP` Bypass**:
   When `mac80211` creates a monitor interface (`IEEE80211_HW_WANT_MONITOR_VIF`), `rtw89` allocates a hardware port and MACID, but never sends H2C table setup commands. The firmware scheduler therefore keeps the transmit queue permanently paused.
   Attempting to join as a client (`RTW89_SELF_ROLE_CLIENT`) triggers internal firmware 5-second beacon-loss disconnect timers. However, registering the role via `rtw89_fw_h2c_join_info()` with `RTW89_SELF_ROLE_AP` and `RTW89_NET_TYPE_NO_LINK`, followed by unpausing `mac_id` via `rtw89_fw_h2c_macid_pause()` and clearing `R_AX_MACID_SLEEP_0` (`0xC2C0`), successfully activates the scheduler queue without sending AP beacons.

3. **Driver TX Path Fixups**:
   - `mac80211` passes `info->control.vif = NULL` on injected Radiotap frames. Stock `rtw89_ops_tx()` called `vif_to_rtwvif(vif)` unconditionally in its variable declarations, triggering immediate kernel panics.
   - When no bitrate is specified in Radiotap, `rates[0].idx = -1` was cast to `(u8)255`, generating an invalid TX descriptor. We map this to default 6.0 Mbps OFDM (`RTW89_HW_RATE_OFDM6`) and bound retries (`tx_cnt_lmt = 1`) for `NO_ACK` frames.

We packaged the solution into an open-source driver extension with an automated AST-based patcher that supports Linux kernels 6.1 through 7.x+:

GitHub Repository: https://github.com/ph4ntom-rev/rtw89-monitor-injection
Architecture Reference: https://github.com/ph4ntom-rev/rtw89-monitor-injection/blob/main/docs/ARCHITECTURE.md

No binary firmware modifications required—runs 100% on the host driver side with official distribution `linux-firmware`.
```

---

### Post B: For `r/Kalilinux` & `r/linuxhardware`
**Title**:
```text
Working Monitor Mode and Packet Injection for Realtek RTL8852AE / rtw89 on Kali Linux (airmon-ng & aireplay-ng fixed!)
```

**Body**:
```markdown
If you bought a modern laptop with a Realtek RTL8852AE (or RTL8852BE) Wi-Fi 6 card, installed Kali Linux, and were disappointed to discover that `airmon-ng` or `aireplay-ng -9` didn't work—there is finally a working fix!

The problem was never a hardware lock or encrypted firmware lock. It was a series of 4 bugs and missing register setups in the Linux kernel `rtw89` driver:
- Band 0 MAC registers dropping Control frames (ACK/RTS/CTS)
- Firmware TX queue remaining paused for monitor interfaces
- Kernel panics when injecting frames due to NULL pointer dereference
- Invalid rate descriptors when tools don't specify a bitrate

We've built an easy-to-use patch and automated installer:
👉 **GitHub**: https://github.com/ph4ntom-rev/rtw89-monitor-injection

### Quick Install on Kali Linux:
```bash
sudo apt update && sudo apt install -y linux-headers-$(uname -r) linux-source build-essential python3
git clone https://github.com/ph4ntom-rev/rtw89-monitor-injection.git
cd rtw89-monitor-injection

# Extract kernel source and compile patched module
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz --strip-components=6 -C ~/rtw89_src --wildcards '*/drivers/net/wireless/realtek/rtw89/*'
chmod +x scripts/*.sh scripts/*.py
./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

### Test Injection:
```bash
sudo airmon-ng check kill
sudo airmon-ng start wlan0
sudo aireplay-ng -9 wlan0mon
```

100% safe: It does not modify your firmware binary and can be uninstalled with a single command (`./scripts/build_module.sh --uninstall`).
```

---

## 3. Hacker News (Show HN)

**Title**:
```text
Show HN: Enabling Monitor Mode and Raw TX on Realtek 802.11ax (rtw89) via driver hooks
```

**URL**: `https://github.com/ph4ntom-rev/rtw89-monitor-injection`

**Text**:
```markdown
Realtek's mainline Linux Wi-Fi 6 driver (`rtw89`, supporting RTL8852AE/BE/CE) has long had a reputation for broken monitor mode and impossible packet injection. Common wisdom in wireless security forums held that Realtek's closed-source RISC-V firmware deliberately disabled promiscuous transmission and reception.

We investigated the driver and hardware interaction and found that the firmware does not restrict this:
1. Promiscuous RX was blocked by Band 0 hardware MAC registers (`R_AX_RX_FLTR_OPT` 0xCE20, `R_AX_CTRL_FLTR` 0xCE24) which drop non-matching BSSID and Control packets before DMA.
2. Raw packet transmission stalled because the driver never sent H2C join commands for standalone monitor interfaces, leaving the MACID paused in the firmware scheduler. Configuring a non-associated `RTW89_SELF_ROLE_AP` with `RTW89_NET_TYPE_NO_LINK` unpauses hardware queues without triggering client beacon-loss timers or broadcasting beacons.
3. Transmitting raw Radiotap frames caused kernel NULL pointer dereferences in `rtw89_ops_tx()` and descriptor rate calculation errors when bitrates were omitted.

We created an open-source driver extension that hooks these paths cleanly, requiring zero modifications to the vendor firmware binary. It supports Linux kernels 6.1 through 7.x+.

Code and technical architecture write-up:
https://github.com/ph4ntom-rev/rtw89-monitor-injection
```

---

## 4. Habr (Хабр) Article Draft (Russian)

**Хабы**: Реверс-инжиниринг, Информационная безопасность, Разработка под Linux  
**Заголовок**:  
`Реверс-инжиниринг прошивки Wi-Fi 6 адаптера Realtek RTL8852AE в IDA Pro: включаем Monitor Mode и Packet Injection без модификации бинарника`

**Аннотация**:  
Пользователи современных ноутбуков с Wi-Fi 6 чипсетами Realtek RTL8852AE/BE годами сталкивались с тем, что в Linux и Kali Linux невозможно запустить `airmon-ng` и инъекцию пакетов (`aireplay-ng`). Считалось, что виновата закрытая прошивка с архитектурой RISC-V. В этой статье мы подробно разберём, как с помощью IDA Pro и анализа регистров выяснилось, что прошивка ничего не блокирует — дело было в четырех скрытых ошибках драйвера ядра Linux `rtw89`.

**Структура статьи**:
1. **Введение**: Проблема Wi-Fi 6 чипов Realtek в Linux. Жалобы в репозиториях `lwfinger/rtw89`.
2. **Анализ аппаратных регистров MAC в IDA Pro**:
   - Регистры `R_AX_RX_FLTR_OPT` (`0xCE20`), `R_AX_CTRL_FLTR` (`0xCE24`). Почему адаптер не видел RTS/CTS/ACK.
   - Магическая маска `0x55555555` и обход фильтрации чужих BSSID.
3. **Обход блокировки TX в планировщике прошивки**:
   - Архитектура H2C (Host-to-Card) команд в `rtw89`.
   - Почему роль клиента (`RTW89_SELF_ROLE_CLIENT`) вызывает таймаут маяков каждые 5 секунд.
   - Элегантный обход: виртуальная роль точки доступа `RTW89_SELF_ROLE_AP` с типом линка `RTW89_NET_TYPE_NO_LINK` и снятие паузы с `mac_id` через `rtw89_fw_h2c_macid_pause()`.
4. **Исправление багов в `mac80211` драйвера ядра**:
   - `NULL pointer dereference` в `rtw89_ops_tx()` при инъекции кадров Radiotap.
   - Обработка `rates[0].idx = -1` и настройка дескриптора передачи `rtw89_core_tx_update_desc_info()`.
5. **Тестирование на реальном железе**:
   - Сборка и установка через DKMS/Kbuild.
   - Запуск `airmon-ng` и успешный тест инъекции `aireplay-ng -9`.
   - Захват 802.11 Control frames в Wireshark.
6. **Выводы и исходный код**:
   - Ссылка на репозиторий: `https://github.com/ph4ntom-rev/rtw89-monitor-injection`

---

## 5. Linux-Wireless RFC Patch Template

**To**: `linux-wireless@vger.kernel.org`  
**Cc**: `pkshih@realtek.com`, `kvalo@kernel.org`  
**Subject**: `[PATCH RFC net-next 0/2] wifi: rtw89: enable full monitor mode RX and raw packet injection`

```text
From: ph4ntom-rev <maintainer@ph4ntom.dev>
Date: Sun, 1 Nov 2026 12:00:00 +0000
Subject: [PATCH RFC net-next 0/2] wifi: rtw89: enable full monitor mode RX and raw packet injection

Hi Ping-Ke, Kalle, and linux-wireless maintainers,

This RFC patchset addresses long-standing limitations in rtw89 when running
mac80211 monitor interfaces (NL80211_IFTYPE_MONITOR) and transmitting injected
diagnostic frames (IEEE80211_TX_CTL_INJECTED) on Realtek 802.11ax PCIe chips
(tested on RTL8852AE).

Currently, creating a monitor interface on rtw89 exhibits several limitations:
1. Band 0 MAC RX registers (R_AX_RX_FLTR_OPT 0xCE20, R_AX_CTRL_FLTR 0xCE24,
   and R_AX_DATA_FLTR 0xCE2C) filter out 802.11 Control frames and non-matching
   BSSID unicast data before reaching host rings.
2. Injected Radiotap frames from mac80211 pass info->control.vif = NULL, which
   triggers a NULL pointer dereference in rtw89_ops_tx() and rtw89_core_tx_write().
3. Injected frames with default rates (rates[0].idx = -1) write an invalid
   rate index and omit desc_info->en_wd_info.
4. The monitor mac_id TX queue remains paused in the firmware scheduler unless
   H2C default CMAC/DMAC tables and role context are configured.

This patchset:
- Configures Band 0 MAC RX filter registers in promiscuous mode when a monitor
  VIF is active.
- Safely resolves monitor VIF prior to vif_to_rtwvif() evaluation.
- Fixes default injection rate index mapping to RTW89_HW_RATE_OFDM6 and bounds
  hardware retries when IEEE80211_TX_CTL_NO_ACK is set.
- Initializes CMAC/DMAC tables and registers a non-associated AP role
  (RTW89_SELF_ROLE_AP + RTW89_NET_TYPE_NO_LINK) to activate the MACID context
  without triggering client beacon loss timers or beacon transmissions.

Feedback and suggestions on the cleanest upstream integration are welcome.

Reference community repository:
https://github.com/ph4ntom-rev/rtw89-monitor-injection
```
