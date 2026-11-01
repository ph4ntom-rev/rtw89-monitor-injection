# Realtek RTL8852AE / rtw89 — Патч режима мониторинга (Monitor Mode) и инъекции пакетов (Packet Injection)

[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)
[![Linux Kernel](https://img.shields.io/badge/Linux-6.1%20--%207.x%2B-success.svg)](docs/ARCHITECTURE.md)
[![Kali Linux](https://img.shields.io/badge/Kali%20Linux-Проверено-darkviolet.svg)](README_RU.md#быстрый-старт-в-kali-linux--ubuntu--debian)
[![Packet Injection](https://img.shields.io/badge/Packet%20Injection-IEEE80211__TX__CTL__INJECTED-brightgreen.svg)](docs/ARCHITECTURE.md)
[![Firmware Safety](https://img.shields.io/badge/Firmware-100%25%20Stock%20Signed-orange.svg)](README_RU.md#правовая-информация-и-соответствие-регуляторным-нормам)

**[English](README.md) | [Русская документация](README_RU.md) | [Техническая архитектура (EN)](docs/ARCHITECTURE.md)**

Патчсет и модуль расширения для штатного открытого драйвера ядра Linux **`rtw89`** (**Realtek RTL8852AE** PCIe `[10ec:8852]`, а также совместимых чипсетов **RTL8852BE** и **RTL8852CE**).

Обеспечивает полноценную поддержку режима монитора `mac80211` (**`NL80211_IFTYPE_MONITOR`**) с захватом кадров с метаданными Radiotap для **Wireshark** / **`tcpdump`**, работу утилит **`airmon-ng`** / **`airodump-ng`**, а также отправку сырых кадров и инъекцию пакетов (**`IEEE80211_TX_CTL_INJECTED`** / **`aireplay-ng`**).

Работает **на 100% на стороне драйвера ОС Linux** со штатной неизменённой подписанной прошивкой из пакета `linux-firmware`. **Модификация бинарных файлов прошивки не требуется.**

---

## В чём была проблема RTL8852AE и rtw89

Тысячи пользователей Linux и Kali Linux сталкиваются с тем, что в современных ноутбуках с адаптерами Realtek RTL8852AE не работает `airmon-ng` и инъекция пакетов, а в репозиториях `lwfinger/rtw89` годами висят вопросы без ответа:
* *«Does RTL8852AE support monitor mode or packet injection?»*
* *«airodump-ng не видит пакеты или зависает»*
* *«При попытке отправки кадров падает ядро (kernel NULL pointer dereference)»*

В результате реверс-инжиниринга регистров и протокола общения хоста с RISC-V контроллером прошивки были выявлены и устранены **четыре ключевые причины**:

1. **Аппаратная фильтрация кадров MAC RX и Address CAM (`reg.h` / `mac.c`)**
   * *Проблема*: Регистры Band 0 MAC по умолчанию сбрасывают кадры Control (ACK, RTS, CTS, BlockAck) и чужие Data-кадры, не совпавшие с `ADDR_CAM`, а сканирование может оставлять фильтр на паузе (`R_AX_PKT_CTRL_B0_PAUSE`).
   * *Решение*: Модуль `rtw89_mon` настраивает регистры `R_AX_RX_FLTR_OPT` (`0xCE20`), `R_AX_CTRL_FLTR` (`0xCE24`), `R_AX_MGNT_FLTR` (`0xCE28`), `R_AX_DATA_FLTR` (`0xCE2C`) маской `0x55555555`, учитывает `FIF_FCSFAIL` и восстанавливает фильтр при смене каналов (`rtw89_set_channel()`).
2. **Паника ядра из-за разыменования `NULL`-указателя `vif` при инъекции (`mac80211.c`, `core.c`)**
   * *Проблема*: В `rtw89_ops_tx()` и `rtw89_core_tx_write()` макрос `vif_to_rtwvif(vif)` вызывался до проверки `info->control.vif != NULL`.
   * *Решение*: Функция `rtw89_mon_resolve_tx_vif()` безопасно разрешает активный мониторный интерфейс до разыменования.
3. **Некорректный индекс скорости и пропуск `TXWD_INFO` (`core.c`)**
   * *Проблема*: При отсутствии явной скорости подсистема `mac80211` выставляет знаковое значение `s8 rates[0].idx = -1`, которое приводилось к `u8` (`255`), вызывая сбой дескриптора передачи.
   * *Решение*: `rtw89_mon_fixup_tx_desc()` выставляет корректную скорость (`RTW89_HW_RATE_OFDM6` — 6 Мбит/с), включает флаги `en_wd_info`, `use_rate`, `dis_data_fb` и ограничивает повторы (`tx_cnt_lmt = 1`) для кадров с флагом `IEEE80211_TX_CTL_NO_ACK`.
4. **Неинициализированный контекст планировщика `mac_id` в прошивке (`fw.h`)**
   * *Проблема*: Выделенный для монитора `mac_id` оставался на паузе в планировщике прошивки, блокируя передачу.
   * *Решение*: `rtw89_mon_add_vif_post()` инициализирует таблицы `h2c_default_cmac_tbl` / `h2c_default_dmac_tbl`, регистрирует контекст через `rtw89_fw_h2c_join_info()` с ролью `RTW89_SELF_ROLE_AP` и типом `RTW89_NET_TYPE_NO_LINK`, снимает паузу через `rtw89_fw_h2c_macid_pause()` и сбрасывает бит в `R_AX_MACID_SLEEP_0` (`0xC2C0`).

---

## Совместимость с ядрами Linux

Скрипт установки `scripts/patch_rtw89.py` автоматически определяет версию драйвера:

| Поколение API | Версии ядер Linux | Архитектура VIF и блокировок | Примеры дистрибутивов |
| :--- | :--- | :--- | :--- |
| **API Gen 1** | `Linux 6.1 – 6.10` | `struct rtw89_vif`, `rtwdev->mutex` | Debian 12 (Bookworm), Ubuntu 22.04 LTS |
| **API Gen 2** | `Linux 6.11 – 7.x+` и `lwfinger/rtw89` | MLO `struct rtw89_vif_link`, `pure_monitor_mode_vif`, `wiphy_lock` | Kali Linux 2024+, Ubuntu 24.04+, Arch Linux, Fedora |

---

## Быстрый старт в Kali Linux / Ubuntu / Debian

### 1. Установка заголовков ядра и утилит сборки
```bash
sudo apt update
sudo apt install -y linux-headers-$(uname -r) linux-source build-essential python3
```

### 2. Сборка и установка пропатченного модуля
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

---

### 3. Запуск режима монитора (`airmon-ng` или `iw`)

#### Способ А: Через `airmon-ng` (Kali Linux)
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

---

### 4. Проверка инъекции пакетов (`aireplay-ng`)
```bash
sudo aireplay-ng -9 wlan0mon
```
Или с помощью встроенного скрипта аппаратной самодиагностики:
```bash
sudo python3 scripts/verify_monitor_diag.py \
  --iface wlan0 \
  --freq 2412 \
  --duration 5 \
  --diag-tx \
  --restore-managed
```

---

## Удаление и возврат к штатному драйверу

```bash
# Удалить модуль из /lib/modules/.../updates и загрузить исходный драйвер
./scripts/build_module.sh --uninstall

# Восстановить исходные файлы rtw89 без патча
python3 scripts/patch_rtw89.py --rtw89-dir ~/rtw89_src --revert
```

---

## Правовая информация и соответствие регуляторным нормам

* **Отсутствие проприетарных файлов прошивки**: Репозиторий **не содержит**, не модифицирует и не распространяет бинарные образы прошивок (`*.bin`). Драйвер работает со штатной подписанной прошивкой из официального пакета `linux-firmware` (`/lib/firmware/rtw89/`).
* **Сохранение регуляторных ограничений (`cfg80211`)**: Частотные планы стран (`wireless-regdb`), запреты каналов (`IEEE80211_CHAN_DISABLED`, `NO_IR`), механизмы обнаружения радаров (`DFS`), таблицы `SAR` и лимиты мощности передатчика в `regd.c`, `sar.c` и `phy.c` **не изменяются**.
* **Лицензия**: Двойная открытая лицензия `GPL-2.0-only OR BSD-3-Clause` ([LICENSE](LICENSE)), совпадающая с лицензией `rtw89` в ядре Linux.
* **Товарные знаки**: Realtek и RTL8852AE являются товарными знаками Realtek Semiconductor Corp. Данный проект не аффилирован с Realtek.
