# Realtek RTL8852AE / rtw89 — Режим мониторинга (Monitor Mode) и инъекция пакетов (`PATCH v4`)

[![Series PATCH v4](https://img.shields.io/badge/Series-PATCH%20v4-blue.svg)](patches/v4/)
[![Linux Kernel Upstream: v3 Submitted / v4 Ready](https://img.shields.io/badge/linux--wireless-v3%20Submitted%20%7C%20v4%20Ready-blue.svg)](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/)
[![checkpatch.pl: strict 0/0/0](https://img.shields.io/badge/checkpatch.pl--strict-0%20errors%20%7C%200%20warnings-brightgreen.svg)](patches/v4/)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)
[![Linux Kernel](https://img.shields.io/badge/Linux-6.1%20--%207.x%2B-success.svg)](docs/ARCHITECTURE.md)
[![Kali Linux](https://img.shields.io/badge/Kali%20Linux-Проверено-darkviolet.svg)](README_RU.md#быстрый-старт-сборка-модуля-в-kali-linux--ubuntu--debian)
[![Packet Injection](https://img.shields.io/badge/Packet%20Injection-IEEE80211__TX__CTL__INJECTED-brightgreen.svg)](docs/ARCHITECTURE.md)
[![Firmware Safety](https://img.shields.io/badge/Firmware-100%25%20Stock%20Signed-orange.svg)](README_RU.md#правовая-информация-и-соответствие-регуляторным-нормам)

**[English](README.md) | [Русская документация](README_RU.md) | [Техническая архитектура (EN)](docs/ARCHITECTURE.md) | [Отчёт о проверке (EN)](docs/PREFLIGHT_VALIDATION.md)**

> [!IMPORTANT]
> ### `PATCH v4` — фильтрация RX, RCU-защита `pure_monitor_mode_vif`, raw injection и восстановление канала
> * **Актуальная проверенная серия**: [`patches/v4/`](patches/v4/) (4 патча поверх апстримного коммита `d48723711bae9b8cdc56719c448ba42fef28cbee`; `checkpatch.pl --strict`: `0/0/0`, сборка `W=1`: `0` предупреждений).
> * **Статус в `linux-wireless`**: предыдущая серия [`PATCH v3`](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/) (`Lore #290240–#290243`) отправлена 5 ноября 2026 года и ожидает ответа мейнтейнеров перед отправкой `PATCH v4` через `--in-reply-to`.
>
> **Состав серии `patches/v4/`:**
> 1. **`v4 1/4`**: `wifi: rtw89: fix RX filter configuration for monitor mode`
> 2. **`v4 2/4`**: `wifi: rtw89: protect pure_monitor_mode_vif with RCU`
> 3. **`v4 3/4`**: `wifi: rtw89: support raw frame injection on standalone monitor interface`
> 4. **`v4 4/4`**: `wifi: rtw89: restore channel after monitor restart on non-chanctx chips`
>
> **Ключевые улучшения `v4` относительно `v3`:**
> * Выделение RCU-аннотации `pure_monitor_mode_vif` (`__rcu`) в отдельный подготовительный патч `2/4` и введение `rtw89_recalc_pure_monitor_vif()` как при добавлении, так и при удалении интерфейсов (включая сценарий отключения `wlan0` при уже поднятом `mon0`).
> * Проверка `!vif || vif->type == NL80211_IFTYPE_MONITOR` через `rcu_dereference(rtwdev->pure_monitor_mode_vif)` в `rtw89_ops_tx()`, что защищает от обращения к удаляемому мониторному `rtwvif` в окне `ieee80211_del_virtual_monitor()`.
> * Удаление избыточных ручных вызовов `rtw89_fw_h2c_join_info()` из `mac80211.c`: при `RTW89_NET_TYPE_NO_LINK` и `RTW89_SELF_ROLE_AP` в `rtw89_vif_type_mapping()` штатные `rtw89_mac_vif_init()` и `rtw89_mac_vif_deinit()` полностью управляют жизненным циклом роли `CCTL` в прошивке.
> * Безопасная обработка коротких (`10`–`16` байт) кадров управления (`ACK`, `RTS`) без чтения `hdr->seq_ctrl`, пропуск `rtw89_traffic_stats_accu()`, `rtw89_wow_parse_akm()`, `rtw89_core_tx_btc_spec_pkt_notify()`, `rtw89_core_tx_update_he_qos_htc()` и `rtw89_core_tx_update_ampdu_info()` на инъектируемых кадрах, сохранение пользовательского `seq_ctrl` (`upd_wlan_hdr = false`, `hw_ssn_sel = 0`, `hw_seq_mode = 0`) без `IEEE80211_TX_CTL_ASSIGN_SEQ` и корректный выбор базовой скорости при `s8 idx < 0`.
> * Перенос восстановления канала при рестарте монитора из `rtw89_ops_add_interface()` в `rtw89_ops_config()` (`IEEE80211_CONF_CHANGE_MONITOR && !rtw89_get_entity_state(rtwdev, RTW89_PHY_0)`), что полностью устраняет двойную 105-мс RF-калибровку при холодном старте.

---

Набор патчей и кросс-версионный модуль расширения для открытого драйвера ядра Linux **`rtw89`**. **Аппаратная верификация выполнена на Realtek RTL8852AE `[10ec:8852]` (`fw 0.13.36.2`, Linux `7.1.5+kali-amd64`); другие чипы семейства `rtw89` и конфигурации DBCC/MAC 1 физически не тестировались.**

Поддерживает захват всех подтипов кадров `mac80211` (**`NL80211_IFTYPE_MONITOR`**) с метаданными Radiotap в **Wireshark** / **`tcpdump`**, работу **`airmon-ng`** / **`airodump-ng`** и инъекцию сырых кадров IEEE 802.11 (**`IEEE80211_TX_CTL_INJECTED`** / **`aireplay-ng`**) на standalone-интерфейсе монитора.

Использует штатную неизменённую подписанную прошивку из официального пакета `linux-firmware`. **Модификация бинарных файлов прошивки не требуется.**

---

## Проверка на реальном железе RTL8852AE (`PATCH v4`, Kali Linux `7.1.5`)

| Проверка | Результат |
| :--- | :--- |
| `checkpatch.pl --strict` (`v4 1/4..4/4`) | `0 errors, 0 warnings, 0 checks` |
| Сборка Kbuild с `W=1` (`PATCH v4` и `patch_rtw89.py`) | `0` предупреждений компилятора |
| Promiscuous RX (`8 с`, 5-й канал / `2432 МГц`) | `21 705` кадров (`127` Beacon, `2 413` BlockAck, `1 390` RTS, `3 147` CTS, `2 797` ACK, `11 282` QoS Data) |
| Базовая инъекция `aireplay-ng -9` | `10/10` (`100%`, среднее `12.34 мс`) |
| Граничные тесты raw TX (`10B ACK`, `16B RTS`, `24B AssocReq`, `SN=1337`, `idx=-1`) | Успешная отправка без ошибок и предупреждений ядра |
| `25` циклов `iw dev mon0 del/add` под параллельным raw TX | `13 579` смешанных кадров (`NullFunc` + `10B ACK` + `16B RTS`) во время пересоздания |
| Приём после stress **без** повторного `iw set channel` | `5 248` кадров сразу после `ip link set mon0 up` |
| Инъекция `aireplay-ng -9` после stress | `30/30` (`100%`, среднее `15.56 мс`) |
| Трассировка `ftrace` (`rtw89_set_channel` / `rtw89_chip_rfk_channel`) | Ровно `1` вызов настройки канала и `1` RFK при холодном старте, `down/up` и `del/add/up` |
| Здоровье ядра (`dmesg`) и возврат `wlan0` в `managed` | `0` ошибок/предупреждений; штатное переподключение к Wi-Fi |

### 1. Захват пакетов IEEE 802.11 Radiotap в Wireshark
Перехват радиоэфира в режиме монитора: приём кадров Management (Beacon, Probe), Control (ACK, BlockAck, RTS, CTS) и чужих Data-кадров с метаданными Radiotap:

![Wireshark RTL8852AE Monitor Mode Capture](docs/screenshots/wireshark_monitor_capture.png)

### 2. Инъекция пакетов (`aireplay-ng -9`)
Диагностический тест инъекции через `aireplay-ng -9` со **100% доставкой кадров** (`30/30` ответов от точки доступа):

![RTL8852AE aireplay-ng Packet Injection Benchmark](docs/screenshots/aireplay_packet_injection.png)

---

## Какие проблемы драйвера `rtw89` устраняет `PATCH v4`

1. **Аппаратная фильтрация MAC RX по `ADDR_CAM`, multicast и BSSID (`v4 1/4`, `mac80211.c`)**
   * *Проблема*: Регистр `R_AX_RX_FLTR_OPT` по умолчанию отбрасывает мультикаст-кадры (без `IFF_PROMISC`), чужие одноадресные кадры, кадры с несовпадающим UID и маяки чужих BSSID, а `rtw89_ops_configure_filter()` сбрасывает `FIF_CONTROL` и `FIF_PSPOLL` из `*new_flags`.
   * *Решение*: В `rtw89_ops_configure_filter()` сохраняются `FIF_CONTROL | FIF_PSPOLL` в `*new_flags`, а при активном `IEEE80211_CONF_MONITOR` в локальной копии `rx_fltr` сбрасываются `B_AX_A_MC`, `B_AX_A_UC_CAM_MATCH`, `B_AX_A_BC_CAM_MATCH`, `B_AX_A_MC_LIST_CAM_MATCH`, `B_AX_A_BCN_CHK_EN` и `B_AX_UID_FILTER_MASK` без изменения базового `rtwdev->hal.rx_fltr`.
2. **Гонки памяти вокруг `pure_monitor_mode_vif` при добавлении и удалении интерфейса (`v4 2/4`, `core.h`, `core.c`, `mac80211.c`)**
   * *Проблема*: `rtw89_ops_tx()` выполняется вне `wiphy_lock`, тогда как добавление и удаление интерфейсов меняют `pure_monitor_mode_vif`.
   * *Решение*: Указатель `pure_monitor_mode_vif` переведён на RCU (`__rcu`), пересчитывается через `rtw89_recalc_pure_monitor_vif()` как при добавлении, так и при удалении интерфейса, а при удалении монитора обнуляется через `RCU_INIT_POINTER(..., NULL)` с барьером `synchronize_rcu()` до освобождения аппаратных ресурсов `mac_id` и CAM.
3. **Разыменование `NULL` / мониторного `vif`, сброс роли в прошивке и ошибки дескриптора инъекции (`v4 3/4`, `core.c`, `mac80211.c`)**
   * *Проблема*:
     1. При отсутствии `local->monitor_sdata` `mac80211` передаёт `info->control.vif == NULL` (что вызывало разыменование смещения от `NULL` в `vif_to_rtwvif(vif)`), а в `ieee80211_del_virtual_monitor()` вызывается `drv_remove_interface()` до обнуления `local->monitor_sdata`.
     2. В `rtw89_vif_type_mapping()` для `NL80211_IFTYPE_MONITOR` оставалась роль `RTW89_SELF_ROLE_CLIENT` (`0`). Во время `rtw89_mac_vif_init()` обработчик `H2C_FUNC_MAC_JOININFO` в 32-битной `MIPS32`/`MIPS16e` прошивке `RTL8852AE` воспринимал `RTW89_NET_TYPE_NO_LINK` как отключение клиента, не инициализировал запись `CCTL` в SRAM (`0xb8840000 + macid * 32`) и молча отбрасывал TX-дескрипторы.
     3. Короткие Control-кадры (`10`–`16` байт) не имеют поля `seq_ctrl`, `rtw89_traffic_stats_accu()` и `rtw89_core_tx_btc_spec_pkt_notify()` интерпретировали 802.11 MAC-заголовок как `ip_hdr`/`udp_hdr`, `rtw89_wow_parse_akm()` разбирал инъектируемые Association Request, беззнаковый `u8 idx` превращал `-1` в `0xff`, а `upd_wlan_hdr`/`hw_seq_mode` перезаписывали номера последовательностей.
   * *Решение*: Подстановка `pure_monitor_mode_vif` для `!vif || vif->type == NL80211_IFTYPE_MONITOR` под `rcu_read_lock()` с проверкой `skb->len >= ieee80211_hdrlen(fc)` и записью `info->control.vif`; назначение `RTW89_NET_TYPE_NO_LINK` и `RTW89_SELF_ROLE_AP` в `rtw89_vif_type_mapping()`; маршрутизация не-Data кадров в `RTW89_CORE_TX_TYPE_MGMT`; защита заголовков `< 24` байт и пропуск `traffic_stats_accu`, `wow_parse_akm`, `btc_spec_pkt_notify`, `update_he_qos_htc` и `update_ampdu_info` для `IEEE80211_TX_CTL_INJECTED`; знаковый `s8 idx` с резервной скоростью `data_retry_lowest_rate` / `OFDM6` (`IEEE80211_TX_CTL_NO_CCK_RATE`); `upd_wlan_hdr = false`, `hw_ssn_sel = 0` и `hw_seq_mode = 0` без `IEEE80211_TX_CTL_ASSIGN_SEQ` и `tx_cnt_lmt = 1` при `IEEE80211_TX_CTL_NO_ACK`.
4. **Потеря канала после `down/up` или пересоздания монитора на чипах без `chanctx` (`v4 4/4`, `mac80211.c`)**
   * *Проблема*: При остановке последнего интерфейса `rtw89_ops_stop()` сбрасывает активность `RTW89_PHY_0`. Повторный подъём монитора на том же канале присылает только `IEEE80211_CONF_CHANGE_MONITOR` без `IEEE80211_CONF_CHANGE_CHANNEL`.
   * *Решение*: В `rtw89_ops_config()` при включении `IEEE80211_CONF_MONITOR`, `!rtwdev->chip->support_chanctx_num` и неактивном `RTW89_PHY_0` восстанавливается `hw->conf.chandef` и вызывается `rtw89_set_channel()`.

> [!NOTE]
> **Режим standalone-монитора для инъекции**: Инъекция сырых пакетов предназначена для **standalone**-интерфейса монитора (переведите `wlan0` в `down` перед работой через `mon0` либо переключите сам `wlan0` в `type monitor`). При одновременно поднятом клиентском или AP-интерфейсе `pure_monitor_mode_vif` равен `NULL`, и сырые кадры безопасно отбрасываются без падения ядра.

---

## Быстрый старт: Применение патчей `PATCH v4` через `git am`

Применяйте патчи `0001`–`0004` (файл `v4-0000-cover-letter.patch` — сопроводительное письмо):

```bash
git clone https://github.com/ph4ntom-rev/rtw89-monitor-injection.git
cd /path/to/linux-source-tree

git am /path/to/rtw89-monitor-injection/patches/v4/v4-000[1-4]-*.patch

# Проверить коммиты:
git log -n 4 --oneline
```

---

## Быстрый старт: Сборка модуля в Kali Linux / Ubuntu / Debian

### 1. Установка заголовков ядра и инструментов сборки
```bash
sudo apt update
sudo apt install -y linux-headers-$(uname -r) linux-source build-essential python3
```

### 2. Сборка и установка обновлённого модуля
```bash
git clone https://github.com/ph4ntom-rev/rtw89-monitor-injection.git
cd rtw89-monitor-injection

mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz \
  --strip-components=6 -C ~/rtw89_src \
  --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

chmod +x scripts/*.sh scripts/*.py
./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

*(Для автоматической пересборки при обновлении ядра также подготовлен профиль [`dkms.conf`](dkms.conf).)*

### 3. Запуск режима монитора (`airmon-ng` или `iw`)

#### Способ А: Через `airmon-ng`
```bash
sudo airmon-ng check kill
sudo airmon-ng start wlan0
sudo airodump-ng wlan0mon
```

#### Способ Б: Через `iw` и `tcpdump` / `Wireshark`
```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq 2412

# Захват кадров 802.11 с Radiotap:
sudo tcpdump -i wlan0 -I -n -e -c 30
```

### 4. Проверка инъекции пакетов (`aireplay-ng`)
```bash
sudo aireplay-ng -9 wlan0mon
```
Или через встроенные скрипты проверки:
```bash
sudo python3 scripts/verify_monitor_diag.py --iface wlan0 --freq 2412 --duration 5 --diag-tx --restore-managed
```

---

## Удаление и возврат к штатному драйверу

```bash
./scripts/build_module.sh --uninstall
python3 scripts/patch_rtw89.py --rtw89-dir ~/rtw89_src --revert
```

---

## Правовая информация и соответствие регуляторным нормам

* **Отсутствие проприетарных файлов прошивки**: Репозиторий **не содержит**, не модифицирует и не распространяет бинарные образы прошивок (`*.bin`). Драйвер работает со штатной подписанной прошивкой из пакета `linux-firmware` (`/lib/firmware/rtw89/`).
* **Сохранение регуляторных ограничений (`cfg80211`)**: Частотные планы стран (`wireless-regdb`), запреты каналов (`IEEE80211_CHAN_DISABLED`, `NO_IR`), механизмы `DFS`, таблицы `SAR` и лимиты мощности передатчика в `regd.c`, `sar.c` и `phy.c` **не изменяются**.
* **Лицензия**: Двойная открытая лицензия `GPL-2.0-only OR BSD-3-Clause` ([LICENSE](LICENSE), [LICENSES/GPL-2.0-only.txt](LICENSES/GPL-2.0-only.txt), [LICENSES/BSD-3-Clause.txt](LICENSES/BSD-3-Clause.txt)), совпадающая с лицензией `drivers/net/wireless/realtek/rtw89/` в ядре Linux.
* **Товарные знаки**: Realtek и RTL8852AE являются товарными знаками Realtek Semiconductor Corp. Проект не аффилирован с Realtek Semiconductor Corp.
