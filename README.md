# ESP32 KeyMaster (Cortex Link)

BLE server and USB-serial bridge for the Waveshare ESP32-S3-LCD-1.47 development board. Serves as **Cortex Link** — a transparent USB-to-BLE bridge for the [Cortex](https://github.com/turfptax/cortex) wearable AI memory system. Includes a 172x320 ST7789 display for real-time status monitoring and OTA updates via WiFi using [ugit](https://github.com/turfptax/ugit).

## Hardware

- [Waveshare ESP32-S3-LCD-1.47](https://www.waveshare.com/esp32-s3-lcd-1.47.htm) (ESP32-S3, 8MB PSRAM, 16MB Flash, BLE 5, 172x320 ST7789 display)

### Pin Map

| Peripheral | Bus | GPIOs |
|-----------|-----|-------|
| Display | SPI(2) | SCK=40, MOSI=45, CS=42, DC=41, RST=39, BL=48 |
| SD Card | SPI(1) | CLK=14, MOSI=15, MISO=16, CS=21 |
| NeoPixel LED | WS2812B | 38 |
| BOOT Button | GPIO | 0 (active LOW) |

## Features

- BLE GATT server with TX/RX characteristics (echo test)
- 172x320 color display showing connection status and event log
- SD card reader/writer for key storage (FAT filesystem)
- NeoPixel RGB LED for visual status (blue=advertising, green=connected, red=error)
- BOOT button input with short/long press detection
- OTA updates from this GitHub repo via ugit (manual trigger in REPL)
- Graceful fallback -- works offline, works without display, SD card, or LED

## First-Time Setup

### 1. Flash MicroPython

```bash
pip install esptool mpremote
# Hold BOOT, press RESET, release both to enter boot mode
esptool.py --chip esp32s3 --port COM4 erase_flash
esptool.py --chip esp32s3 --port COM4 write_flash -z 0 ESP32_GENERIC_S3-SPIRAM_OCT-*.bin
```

Download firmware from: https://micropython.org/download/ESP32_GENERIC_S3/

### 2. Install Libraries

```bash
mpremote connect COM4 mip install aioble
mpremote connect COM4 mip install github:turfptax/ugit
```

### 3. Upload Project Files

```bash
mpremote connect COM4 cp boot.py :boot.py
mpremote connect COM4 cp main.py :main.py
mpremote connect COM4 cp ble_server.py :ble_server.py
mpremote connect COM4 cp display_manager.py :display_manager.py
mpremote connect COM4 cp st7789py.py :st7789py.py
mpremote connect COM4 cp vga1_8x16.py :vga1_8x16.py
mpremote connect COM4 cp sdcard.py :sdcard.py
mpremote connect COM4 cp sd_manager.py :sd_manager.py
mpremote connect COM4 cp led_manager.py :led_manager.py
mpremote connect COM4 cp button.py :button.py
```

### 4. Configure OTA Updates

Run once in the MicroPython REPL:

```python
import ugit
ugit.create_config(
    ssid='YourWiFiSSID',
    password='YourWiFiPassword',
    user='turfptax',
    repository='cortex-link',
    ignore=['/lib', '/ble_secrets.json']
)
```

The `ignore` list protects device-only files from being deleted during sync.

After this, trigger updates manually in REPL:
```python
import ugit; ugit.wificonnect(); ugit.pull_all()
```

### 5. Reset and Run

Press the RESET button. The device will:
1. Start the BLE server
2. Mount SD card (if inserted)
3. Initialize NeoPixel LED (blue = advertising)
4. Start BOOT button monitor
5. Show status on the display
6. Advertise as "KeyMaster"

## Testing BLE

1. Install [nRF Connect](https://play.google.com/store/apps/details?id=no.nordicsemi.android.mcp) on your phone
2. Scan and connect to "KeyMaster"
3. Expand the service (`a0e1b2c3-...4e50`)
4. Subscribe to notifications on TX characteristic (`...4e51`)
5. Write text to RX characteristic (`...4e52`)
6. You should see your message echoed back

## SD Card

- Insert a FAT32-formatted microSD card before powering on
- Mounts automatically at `/sd`
- Press BOOT button (short press) to check SD card status on display
- If no card is inserted, the device starts normally without SD functionality

## NeoPixel LED Status

| Color | Meaning |
|-------|---------|
| White | Device booting |
| Blue | BLE advertising (waiting for connection) |
| Green | BLE connected |
| Cyan | Data received |
| Red | Disconnected or error |
| Yellow | SD card activity |

## BOOT Button

| Action | Duration | Function |
|--------|----------|----------|
| Short press | < 1 second | Show device status info |
| Long press | >= 1 second | Reserved for future use |

## Project Structure

| File | Purpose |
|------|---------|
| `boot.py` | System init, USB-CDC safety delay |
| `main.py` | Application entry point, wires all peripherals to BLE |
| `ble_server.py` | BLE GATT server using aioble |
| `display_manager.py` | ST7789 display wrapper with status zones |
| `st7789py.py` | ST7789 display driver (pure Python) |
| `vga1_8x16.py` | Bitmap font (8x16 pixels) |
| `sdcard.py` | SD card SPI driver (from micropython-lib) |
| `sd_manager.py` | SD card mount/unmount and file operations |
| `led_manager.py` | NeoPixel RGB LED controller |
| `serial_bridge.py` | USB serial ↔ BLE bridge with chunking protocol |
| `menu_ui.py` | Interactive menu system (single button navigation) |
| `key_store.py` | Encrypted key storage on SD card |
| `button.py` | BOOT button async handler with debounce |

## Cortex System Integration

When used as **Cortex Link**, this ESP32 acts as a transparent bidirectional bridge:

```
Computer ──USB Serial──> ESP32 (this firmware) ──BLE──> Pi Zero 2 W (Cortex Core)
```

- All USB serial lines are forwarded over BLE to the Pi, and vice versa
- Messages > 199 bytes are automatically chunked using the `CHUNK:n/N:data` protocol
- The ESP32 does not parse or modify message content — it is fully transparent
- WiFi is the preferred transport (direct HTTP to Pi); BLE via this dongle is the fallback

Related repos:
- [cortex](https://github.com/turfptax/cortex) — MCP server, CLI, and daemon (runs on PC)
- [cortex-core](https://github.com/turfptax/cortex-core) — Pi Zero service, database, HTTP API

## License

MIT
