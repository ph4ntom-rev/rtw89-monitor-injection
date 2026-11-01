# rtw89-8852ae-monitor (Русская документация)

**[English README](../README.md) | [Русская версия в корне](../README_RU.md) | [Техническая архитектура (EN)](ARCHITECTURE.md)**

Расширение и набор исправлений для штатного открытого драйвера Linux **`rtw89`** (`RTW89_CHIP_AX` / **Realtek RTL8852AE** PCIe `[10ec:8852]`), обеспечивающее полноценную поддержку режима мониторинга `mac80211` (**`NL80211_IFTYPE_MONITOR`**) с захватом метаданных Radiotap в Wireshark / `tcpdump` и диагностической отправкой тестовых кадров IEEE 802.11 (`IEEE80211_TX_CTL_INJECTED`).

Модуль работает **исключительно на стороне драйвера ОС Linux** со штатной неизменённой подписанной прошивкой из пакета `linux-firmware`.

---

## Какие ограничения и ошибки `rtw89` исправляет проект

1. **Настройка аппаратного MAC-фильтра приёма (`reg.h` / `mac.c`)**
   В базовой конфигурации регистры Band 0 MAC отбрасывают управляющие кадры Control (RTS, CTS, ACK, BlockAck) и чужие одноадресные кадры Data, не совпавшие с таблицей `ADDR_CAM`, а после аппаратного сканирования фильтр пакетов Band 0 может оставаться на паузе. Модуль `rtw89_mon` настраивает регистры `R_AX_RX_FLTR_OPT`, `R_AX_CTRL_FLTR`, `R_AX_MGNT_FLTR` и `R_AX_DATA_FLTR` (`0x55555555`), учитывает флаг `FIF_FCSFAIL` и автоматически восстанавливает фильтр при переключении радиоканалов (`rtw89_set_channel()`).
2. **Устранение разыменования `NULL`-указателя `vif` при отправке через монитор (`mac80211.c`, `core.c`)**
   В функциях `rtw89_ops_tx()` и `rtw89_core_tx_write()` макрос `vif_to_rtwvif(vif)` вызывался до проверки `info->control.vif != NULL` (в мониторном тракте `mac80211` поле `control.vif` может быть `NULL`). Функция `rtw89_mon_resolve_tx_vif()` безопасно подставляет активный мониторный `vif`.
3. **Исправление выбора скорости и `TXWD_INFO` в `rtw89_core_tx_update_injection()` (`core.c`)**
   При отсутствии явной скорости в заголовке Radiotap подсистема `mac80211` передаёт знаковое значение `s8 rates[0].idx = -1`, которое в базовом драйвере приводилось к `u8` (`255` — некорректный индекс скорости), а флаг `en_wd_info` не выставлялся для не-Management кадров. Функция `rtw89_mon_fixup_tx_desc()` выбирает валидную базовую скорость (`RTW89_HW_RATE_OFDM6` — 6 Мбит/с), активирует `en_wd_info`, `use_rate`, `dis_data_fb` и ограничивает число повторов (`tx_cnt_lmt = 1`) при флаге `IEEE80211_TX_CTL_NO_ACK`.
4. **Инициализация контекста `mac_id` для неассоциированного мониторного интерфейса (`fw.h`)**
   При создании интерфейса `NL80211_IFTYPE_MONITOR` (`IEEE80211_HW_WANT_MONITOR_VIF`) модуль инициализирует таблицы `h2c_default_cmac_tbl` / `h2c_default_dmac_tbl`, регистрирует контекст `mac_id` через штатный вызов `rtw89_fw_h2c_join_info()` (`RTW89_SELF_ROLE_AP` + `RTW89_NET_TYPE_NO_LINK`) и снимает паузу планировщика через `rtw89_fw_h2c_macid_pause()`.

---

## Совместимость с ядрами Linux

Скрипт `scripts/patch_rtw89.py` автоматически определяет поколение внутреннего API драйвера `rtw89`:
* **API Gen 1 (`Linux 6.1 – 6.10`)**: классическая структура `struct rtw89_vif` и синхронизация на `rtwdev->mutex`.
* **API Gen 2 (`Linux 6.11 – 7.x+` и `lwfinger/rtw89`)**: MLO-архитектура `struct rtw89_vif_link`, `pure_monitor_mode_vif` и `wiphy_lock`.

---

## Быстрый старт

### 1. Проверка окружения Linux
```bash
chmod +x scripts/*.sh scripts/*.py
./scripts/build_module.sh --check-env
```

### 2. Сборка и установка модулей (пример для Debian / Ubuntu / Kali)
```bash
sudo apt install linux-headers-$(uname -r) linux-source build-essential

mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz \
  --strip-components=6 -C ~/rtw89_src \
  --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

### 3. Захват кадров 802.11 в Wireshark / `tcpdump` и диагностический тест
```bash
sudo python3 scripts/verify_monitor_diag.py \
  --iface wlan0 \
  --freq 2412 \
  --duration 5 \
  --diag-tx \
  --restore-managed
```

### 4. Удаление и возврат к штатному драйверу дистрибутива
```bash
./scripts/build_module.sh --uninstall
python3 scripts/patch_rtw89.py --rtw89-dir ~/rtw89_src --revert
```

---

## Правовая информация и соответствие регуляторным нормам

* **Отсутствие проприетарных файлов прошивки**: Репозиторий **не содержит**, не модифицирует и не распространяет бинарные образы прошивок (`*.bin`). Драйвер работает со штатной подписанной прошивкой из официального пакета `linux-firmware` (`/lib/firmware/rtw89/`).
* **Сохранение регуляторных ограничений (`cfg80211`)**: Частотные планы стран (`wireless-regdb`), запреты каналов (`IEEE80211_CHAN_DISABLED`, `NO_IR`), механизмы обнаружения радаров (`DFS`), таблицы `SAR` и лимиты выходной мощности передатчика в `regd.c`, `sar.c` и `phy.c` **не изменяются**.
* **Лицензия**: Двойная открытая лицензия `GPL-2.0-only OR BSD-3-Clause` ([LICENSE](../LICENSE)), полностью совпадающая с лицензией подсистемы `drivers/net/wireless/realtek/rtw89/` в основном дереве ядра Linux.
* **Товарные знаки**: Realtek и RTL8852AE являются товарными знаками корпорации Realtek Semiconductor Corp. Данный проект представляет собой независимый открытый набор патчей для подсистемы `mac80211` ядра Linux и не аффилирован с Realtek Semiconductor Corp.
