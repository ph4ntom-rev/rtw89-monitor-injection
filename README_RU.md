# rtw89: исправления режима монитора и инъекции кадров (`PATCH v5`)

[![CI](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml/badge.svg)](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)

**[English](README.md) | [Русский](README_RU.md) | [Архитектура](docs/ARCHITECTURE.md) | [Проверка](docs/PREFLIGHT_VALIDATION.md)**

Патчи для драйвера `rtw89` из основного ядра Linux (Realtek RTL8851B/8852A/8852B/8852C/8922A).
Исправляют аппаратный RX-фильтр в режиме монитора (включая сохранение фильтра после
сканирования, ROC и реинициализации MAC), включают инъекцию сырых 802.11-кадров
(`aireplay-ng`, radiotap TX) на отдельном monitor-интерфейсе и восстанавливают рабочий
канал после перезапуска интерфейса монитора. Прошивка не изменяется.

## Состояние

| | |
| :--- | :--- |
| Текущая серия | [`patches/v5/`](patches/v5/), 10 патчей, база `torvalds/linux` [`d48723711bae`](https://github.com/torvalds/linux/commit/d48723711bae9b8cdc56719c448ba42fef28cbee) (окно слияния 7.4-rc1) |
| Рассылка | v3 [отправлена](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/) 2026-11-05, ждёт ревью. v4 не отправлялась. v5 проверена на прошивке и железе и готова к отправке после ревью v3. |
| Сборка и статика | Каждый патч собирается отдельно; `W=1` и sparse (`C=1`) с `CONFIG_PROVE_LOCKING=y` дают 0 новых предупреждений; `checkpatch.pl --strict` — 0 ошибок/предупреждений; `patch_rtw89.py` проверяется на `v6.1`, `v6.6`, `v6.12` (Gen 1), `v6.15`, `v6.18` (Gen 2) и `v7.4-rc1` с реальной Kbuild-сборкой `make W=1 modules` на LTS `v6.12`, LTS `v6.18` и `v7.4-rc1` в [CI](.github/workflows/ci.yml). |
| Проверка на железе | **RTL8852AE** (`10ec:8852`, прошивка `rtw89/rtw8852a_fw-1.bin` `0.13.36.2`, Linux `7.1.5+kali-amd64`) — верифицирована на прошивке и протестирована вживую на коде **v5**, см. [отчёт](docs/PREFLIGHT_VALIDATION.md). |
| Не проверено на железе | RTL8852B/C/BT, RTL8851B, RTL8922A/D (Wi-Fi 7), USB-адаптеры, DBCC. |

## Серия v5

| # | Патч | Тип |
| :-- | :--- | :--- |
| 1 | `wifi: rtw89: avoid NULL vif dereference in rtw89_ops_tx()` | исправление, `Fixes:` + `Cc: stable` |
| 2 | `wifi: rtw89: handle negative rate index for injected frames` | исправление, `Fixes:` |
| 3 | `wifi: rtw89: fix RX filter configuration for monitor mode` | исправление |
| 4 | `wifi: rtw89: protect pure_monitor_mode_vif with RCU` | RCU и отслеживание единственного monitor-VIF |
| 5 | `wifi: rtw89: use AP self role for monitor interfaces` | инъекция (подтверждено по прошивке `0.13.36.2`) |
| 6 | `wifi: rtw89: transmit frames from a standalone monitor interface` | инъекция |
| 7 | `wifi: rtw89: send injected control frames through the management path` | инъекция |
| 8 | `wifi: rtw89: skip host-side TX hooks for injected frames` | инъекция |
| 9 | `wifi: rtw89: honor ASSIGN_SEQ and NO_ACK for injected frames` | инъекция |
| 10 | `wifi: rtw89: restore channel after monitor restart on non-chanctx devices` | исправление |

Подробное описание каждого патча и out-of-tree слоя `rtw89_mon` приведено в
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) и
[cover letter](patches/v5/v5-0000-cover-letter.patch).

> [!NOTE]
> Инъекция работает только на **отдельном** monitor-интерфейсе: на этой же карте не должно
> быть открытых интерфейсов в режиме станции или точки доступа. Иначе инжектируемые кадры
> безопасно отбрасываются (патчи 1 и 6).

## Использование

### A. Дерево ядра mainline / wireless-next

```bash
cd linux
git am /path/to/rtw89-monitor-injection/patches/v5/v5-00*-wifi-*.patch
```

### B. Ядро дистрибутива, разовая сборка

`scripts/patch_rtw89.py` переносит изменения `v5` в исходники `rtw89` ядра дистрибутива
(`Linux 6.1 - 7.x`) и завершается с явной ошибкой, если любой обязательный хук не найден.

```bash
sudo apt install linux-headers-$(uname -r) linux-source build-essential python3
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz --strip-components=6 -C ~/rtw89_src \
    --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

bash scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

`build_module.sh` спрашивает подтверждение перед выгрузкой работающего драйвера (Wi-Fi
ненадолго пропадёт); `--yes` отключает вопрос. Все модули `rtw89` устанавливаются вместе в
`/lib/modules/$(uname -r)/updates/rtw89`, чтобы ядро драйвера, модуль шины и модули чипов
всегда собирались из одних исходников. Откат: `bash scripts/build_module.sh --uninstall`.

**Secure Boot:** неподписанные модули ядро не загрузит. Скрипт определяет Secure Boot через
`mokutil` и останавливается до выгрузки драйвера. Подпись зарегистрированным MOK-ключом:

```bash
bash scripts/build_module.sh --rtw89-dir ~/rtw89_src --install \
    --sign-key /var/lib/shim-signed/mok/MOK.priv --sign-cert /var/lib/shim-signed/mok/MOK.der
```

### C. Ядро дистрибутива через DKMS

```bash
bash scripts/dkms_prepare.sh --source /usr/src/linux-source-7.1.tar.xz
sudo dkms install rtw89-monitor-injection/5.0
```

`dkms_prepare.sh` ограничивает `BUILD_EXCLUSIVE_KERNEL` серией `MAJOR.MINOR` исходного ядра
(поддерживая как релизные, так и `-rc` версии). После перехода на новую серию ядра
запустите `dkms_prepare.sh` повторно с соответствующим архивом `linux-source`. Удаление:
`bash scripts/dkms_prepare.sh --remove`.

### Режим монитора и инъекция

```bash
sudo ip link set wlan0 down
sudo iw dev wlan0 set type monitor
sudo ip link set wlan0 up
sudo iw dev wlan0 set freq 2412
sudo tcpdump -i wlan0 -e -c 30
sudo aireplay-ng -9 wlan0
```

## Структура репозитория

```text
patches/v5/            актуальная серия из 10 патчей + cover letter
patches/v2..v4/        предыдущие версии, сохранены для истории
src/rtw89_mon.{c,h}    вспомогательный модуль, подключаемый к rtw89_core через patch_rtw89.py
scripts/               patch_rtw89.py, build_module.sh, dkms_prepare.sh, dkms_pre_build.sh,
                       verify_hardware.py, verify_monitor_diag.py
dkms/dkms.conf.in      шаблон DKMS для dkms_prepare.sh
tests/                 юнит-тесты интегратора и гигиены серии патчей
docs/                  техническое описание архитектуры и отчёт о проверке на железе
```

## Правовые вопросы и регулирование

- Репозиторий не содержит прошивок и кода вендора; используется штатная подписанная
  прошивка из `linux-firmware`.
- Регуляторные домены, ограничения мощности, флаги DFS и `NO_IR` не изменяются. Вы сами
  отвечаете за соблюдение местных правил использования радиочастот и за то, чтобы
  передавать кадры только в сетях, на тестирование которых у вас есть разрешение.
- Двойная лицензия `GPL-2.0-only OR BSD-3-Clause`, как у драйвера в ядре
  ([LICENSE](LICENSE), [LICENSES/](LICENSES/)).
- Realtek является товарным знаком Realtek Semiconductor Corp. Проект не связан с Realtek.
