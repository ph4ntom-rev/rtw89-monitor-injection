# rtw89: исправления режима монитора и инъекции кадров (`PATCH v5`)

[![CI](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml/badge.svg)](https://github.com/ph4ntom-rev/rtw89-monitor-injection/actions/workflows/ci.yml)
[![License: GPL-2.0 OR BSD-3-Clause](https://img.shields.io/badge/License-GPL--2.0%20OR%20BSD--3--Clause-blue.svg)](LICENSE)

**[English](README.md) | [Русский](README_RU.md) | [Архитектура](docs/ARCHITECTURE.md) | [Проверка](docs/PREFLIGHT_VALIDATION.md)**

Патчи для драйвера `rtw89` из основного ядра Linux (Realtek RTL8851B/8852A/8852B/8852C/8922A).
Они исправляют приём в режиме монитора и включают инъекцию сырых 802.11-кадров
(`aireplay-ng`, radiotap TX) на отдельном monitor-интерфейсе. Прошивка не изменяется.

## Состояние

| | |
| :--- | :--- |
| Текущая серия | [`patches/v5/`](patches/v5/), 10 патчей, база `torvalds/linux` [`d48723711bae`](https://github.com/torvalds/linux/commit/d48723711bae9b8cdc56719c448ba42fef28cbee) (окно слияния 7.4-rc1) |
| Рассылка | v3 [отправлена](https://lore.kernel.org/linux-wireless/20261104203051.564-1-owner@ph4ntom-rev.uz/) 2026-11-05, ждёт ревью. v4 не отправлялась. v5 будет отправлена после ревью v3 и повторной проверки на железе. |
| Сборка | Каждый патч собирается отдельно; `W=1` и sparse (`C=1`) не дают новых предупреждений; `checkpatch.pl --strict` чистый. Проверяется в [CI](.github/workflows/ci.yml). |
| Проверка на железе | **Только RTL8852AE** (`10ec:8852`, прошивка 0.13.36.2, Linux 7.1.5), на коде **v4**. v5 на железе ещё не перепроверена, см. [отчёт](docs/PREFLIGHT_VALIDATION.md). |
| Не проверено | RTL8852B/C, RTL8851B, RTL8922A (Wi-Fi 7), USB-адаптеры, DBCC. |

## Серия v5

| # | Патч | Тип |
| :-- | :--- | :--- |
| 1 | `wifi: rtw89: avoid NULL vif dereference in rtw89_ops_tx()` | исправление, `Fixes:` + `Cc: stable` |
| 2 | `wifi: rtw89: handle negative rate index for injected frames` | исправление, `Fixes:` |
| 3 | `wifi: rtw89: fix RX filter configuration for monitor mode` | исправление |
| 4 | `wifi: rtw89: protect pure_monitor_mode_vif with RCU` | подготовка, без изменения поведения |
| 5 | `wifi: rtw89: use AP self role for monitor interfaces` | инъекция, нужно подтверждение от Realtek |
| 6 | `wifi: rtw89: transmit frames from a standalone monitor interface` | инъекция |
| 7 | `wifi: rtw89: send injected control frames through the management path` | инъекция |
| 8 | `wifi: rtw89: skip host-side TX hooks for injected frames` | инъекция |
| 9 | `wifi: rtw89: honor ASSIGN_SEQ and NO_ACK for injected frames` | инъекция |
| 10 | `wifi: rtw89: restore channel after monitor restart on non-chanctx chips` | исправление |

Изменения относительно v4 перечислены в [cover letter](patches/v5/v5-0000-cover-letter.patch).
Главное: v4 отправляла через management-очередь **все** управляющие кадры, включая
BlockAckReq, которые формирует сам mac80211. В v5 это делается только для инжектированных кадров.

> [!NOTE]
> Инъекция работает только на **отдельном** monitor-интерфейсе: на этой же карте не должно
> быть поднятых интерфейсов в режиме станции или точки доступа. Иначе кадры отбрасываются
> (патч 6), как и раньше.

## Использование

### A. Дерево ядра mainline / wireless-next

```bash
cd linux
git am /path/to/rtw89-monitor-injection/patches/v5/v5-00*-wifi-*.patch
```

### B. Ядро дистрибутива, разовая сборка

`scripts/patch_rtw89.py` вносит те же изменения в исходники rtw89 более старого ядра.
Он правит C-код по шаблонам, поэтому работает **по мере возможности**: в CI он проверяется
только на базе серии. Проверяйте вывод каждого шага `[APPLY]`.

```bash
sudo apt install linux-headers-$(uname -r) linux-source build-essential python3
mkdir -p ~/rtw89_src
tar -xf /usr/src/linux-source-*.tar.xz --strip-components=6 -C ~/rtw89_src \
    --wildcards '*/drivers/net/wireless/realtek/rtw89/*'

./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install
```

`build_module.sh` спрашивает подтверждение перед выгрузкой работающего драйвера (Wi-Fi
ненадолго пропадёт); `--yes` отключает вопрос. Все модули rtw89 ставятся вместе в
`/lib/modules/$(uname -r)/updates/rtw89`, чтобы ядро драйвера, модуль шины и модули чипов
всегда были собраны из одних исходников. Откат: `./scripts/build_module.sh --uninstall`.

**Secure Boot:** неподписанные модули ядро не загрузит. Скрипт определяет Secure Boot через
`mokutil` и останавливается до выгрузки драйвера. Подпись зарегистрированным MOK-ключом:

```bash
./scripts/build_module.sh --rtw89-dir ~/rtw89_src --install \
    --sign-key /var/lib/shim-signed/mok/MOK.priv --sign-cert /var/lib/shim-signed/mok/MOK.der
```

### C. Ядро дистрибутива через DKMS

```bash
./scripts/dkms_prepare.sh --source /usr/src/linux-source-7.3.tar.xz
sudo dkms install rtw89-monitor-injection/5.0
```

Исходники rtw89 берутся из одной серии ядра, поэтому DKMS пересобирает модули только для
ядер этой серии (`BUILD_EXCLUSIVE_KERNEL`). Остальные ядра остаются со штатным драйвером.
После перехода на новую серию ядра запустите `dkms_prepare.sh` ещё раз с новым пакетом
`linux-source`. Удаление: `./scripts/dkms_prepare.sh --remove`.

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
patches/v5/            текущая серия (+ cover letter)
patches/v2..v4/        предыдущие версии, для истории
src/rtw89_mon.{c,h}    вспомогательный код, который patch_rtw89.py добавляет в rtw89_core
scripts/               patch_rtw89.py, build_module.sh, dkms_prepare.sh, dkms_pre_build.sh,
                       verify_hardware.py, verify_monitor_diag.py
dkms/dkms.conf.in      шаблон DKMS для dkms_prepare.sh
tests/                 юнит-тесты интегратора и файлов патчей
docs/                  описание архитектуры, отчёт о проверке
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
