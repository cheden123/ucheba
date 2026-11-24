# Исходники мультимедийных компонентов Учёба AI 0.4

Публикуемые EXE и ZIP динамически загружают библиотеки Qt из `_internal/PySide6`. Локальная сборка дополнительно содержит оригинальное колесо PyAV. Публичная сборка исключает PyAV, а также неиспользуемые Qt Virtual Keyboard и Qt PDF вместе с их плагинами и цепочкой зависимостей Qt Quick/QML, используемой только Qt Virtual Keyboard. Каталоги с уведомлениями и текстами лицензий расположены в `licenses` рядом с приложением.

## Faster Whisper публичной сборки

[Исходный код faster-whisper 1.2.1](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1) и [точечный патч упаковки](https://github.com/cheden123/ucheba/blob/v0.4.0/scripts/prepare-public-whisper.py). Патч загружает PyAV только при вызове встроенного декодирования файла. Учёба AI получает 16 кГц PCM через отдельно загружаемый FFmpeg и передаёт движку массив NumPy, а PyAV и связанные кодеки не входят в публичный пакет.

## PyAV 19.0.2 и FFmpeg 9.0.2 в локальной сборке

- [Исходный код PyAV 19.0.2](https://github.com/PyAV-Org/PyAV/archive/refs/tags/v19.0.2.tar.gz)
- [PyAV 19.0.2 Windows x64](https://github.com/PyAV-Org/PyAV/releases/tag/v19.0.2)
- [Тег рецепта FFmpeg, указанный в конфигурации PyAV](https://raw.githubusercontent.com/PyAV-Org/PyAV/v19.0.2/scripts/ffmpeg-latest.json)
- [Рецепт 9.0.2-1, патчи, точные версии и ссылки на источники зависимостей](https://github.com/PyAV-Org/pyav-ffmpeg/archive/refs/tags/9.0.2-1.tar.gz)
- [FFmpeg 9.0.2-1 Windows x64, использованный рецептом PyAV](https://github.com/PyAV-Org/pyav-ffmpeg/releases/download/9.0.2-1/ffmpeg-windows-x86_64.tar.gz)
- [Исходный код FFmpeg 9.0.2 до применения патчей](https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz)

В теге рецепта `scripts/pkg.py` задаёт версии и URL источников библиотек, `patches/` содержит изменения, а `scripts/build-ffmpeg.py` и `.github/workflows/build-ffmpeg.yml` описывают компиляцию для Windows. DLL FFmpeg в комплекте PyAV сообщает LGPL версии 3 или новее.

## PySide6 и Qt 6.12.0 с FFmpeg 9.0.1

- [Исходный код PySide6 6.12.0](https://github.com/pyside/pyside-setup/archive/refs/tags/v6.12.0.tar.gz)
- [Полный исходный архив Qt 6.12.0](https://download.qt.io/official_releases/qt/6.12/6.12.0/single/qt-everywhere-src-6.12.0.tar.xz)
- [Исходный код Qt Multimedia 6.12.0](https://github.com/qt/qtmultimedia/archive/refs/tags/v6.12.0.tar.gz)
- [Атрибуция FFmpeg для точного тега Qt Multimedia 6.12.0](https://github.com/qt/qtmultimedia/blob/v6.12.0/src/3rdparty/ffmpeg/qt_attribution.json)
- [Рецепт сборки FFmpeg для Windows в точном теге Qt](https://github.com/qt/qt5/blob/v6.12.0/coin/provisioning/common/windows/install-ffmpeg.ps1)
- [Общие параметры сборки и версии зависимостей](https://github.com/qt/qt5/tree/v6.12.0/coin/provisioning/common/shared)
- [Исходный код FFmpeg 9.0.1](https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.gz)

DLL FFmpeg в Qt сообщает LGPL версии 2.1 или новее. Исходники, рецепты сборки и лицензионные сведения доступны по зафиксированным версиям выше.

## Компоненты первого запуска

В публичных EXE и ZIP отсутствуют командные программы FFmpeg из сборки Gyan и модель голосовых признаков ERes2Net. При первой обработке они скачиваются напрямую из закреплённых выпусков [Gyan FFmpeg 9.1](https://github.com/GyanD/codexffmpeg/releases/tag/9.1) и [Sherpa ONNX](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models), затем проверяются по SHA-256. Модель сегментации и её лицензия находятся в каталоге `_internal/ucheba/models/diarization`. [Исходник sherpa-onnx 2.0.3](https://github.com/k2-fsa/sherpa-onnx/tree/v2.0.3).
