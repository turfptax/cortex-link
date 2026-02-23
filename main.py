import gc
import time
import asyncio
from ble_server import BLEServer

print("main.py: starting application")

_boot_ticks = time.ticks_ms()

# ---------------------------------------------------------------
# Hardware init -- each peripheral is optional; failures are caught
# so the BLE server always starts.
# ---------------------------------------------------------------

# Display
display = None
try:
    from display_manager import DisplayManager
    display = DisplayManager()
    display.show_startup()
    display.log("Display initialized")
    gc.collect()
    print("main.py: display initialized OK")
except Exception as e:
    print("main.py: display init FAILED:", e)
    import sys
    sys.print_exception(e)

# NeoPixel LED
led = None
try:
    from led_manager import LEDManager
    led = LEDManager(pin=38, brightness=0.3)
    led.status_startup()
    gc.collect()
    print("main.py: LED initialized OK")
except Exception as e:
    print("main.py: LED init FAILED:", e)

# SD card
sd = None
try:
    from sd_manager import SDManager
    sd = SDManager(sck=14, mosi=15, miso=16, cs=21, spi_id=1)
    if sd.mount():
        info = sd.free_space()
        if info:
            free_mb = info[0] / 1_048_576
            total_mb = info[1] / 1_048_576
            print("main.py: SD card {:.1f}/{:.1f} MB free".format(free_mb, total_mb))
            if display:
                display.log("SD: {:.0f}/{:.0f}MB".format(free_mb, total_mb))
        files = sd.list_files()
        print("main.py: SD files:", files)
    else:
        sd = None  # mount failed, treat as unavailable
    gc.collect()
except Exception as e:
    print("main.py: SD init FAILED:", e)
    sd = None

# Key store (requires SD)
key_store = None
try:
    if sd and sd.is_mounted:
        from key_store import KeyStore
        key_store = KeyStore(sd)
        print("main.py: key store initialized OK")
        gc.collect()
except Exception as e:
    print("main.py: key store init FAILED:", e)

# Menu system (requires display)
menu = None
try:
    if display:
        from menu_ui import MenuManager
        menu = MenuManager(display)
        print("main.py: menu system initialized OK")
        gc.collect()
except Exception as e:
    print("main.py: menu init FAILED:", e)

# BOOT button
button = None
try:
    from button import ButtonManager
    button = ButtonManager(
        pin=0,
        on_press=lambda dur: _on_button_press(dur),
        on_long_press=lambda dur: _on_button_long_press(dur),
    )
    gc.collect()
    print("main.py: button initialized OK")
except Exception as e:
    print("main.py: button init FAILED:", e)

# BLE server and bridge references (set in main(), used by callbacks)
server = None
bridge = None


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _log(msg):
    """Log to display if available."""
    if display:
        display.log(msg)


# ---------------------------------------------------------------
# Menu structure builders (lazy -- called on demand)
# ---------------------------------------------------------------

def _build_main_menu():
    from menu_ui import MenuItem
    return [
        MenuItem("Device Info", "sub", _build_device_info),
        MenuItem("Keys", "sub", _build_keys_menu),
        MenuItem("Settings", "sub", _build_settings_menu),
        MenuItem("OTA Update", "sub", _build_ota_menu),
        MenuItem("About", "cb", _show_about),
    ]


def _build_device_info():
    from menu_ui import MenuItem
    items = [
        MenuItem("BLE", "info", _get_ble_info),
        MenuItem("Bridge", "info", _get_bridge_info),
        MenuItem("SD Card", "info", _get_sd_info),
        MenuItem("Uptime", "info", _get_uptime),
        MenuItem("Free RAM", "info", _get_free_ram),
        MenuItem("< Back", "cb", None),
    ]
    return ("Device Info", items)


def _get_ble_info():
    if server and hasattr(server, '_connection') and server._connection:
        return "Connected"
    return "Advertising"


def _get_bridge_info():
    if bridge and server and server.connected:
        return "Active"
    elif bridge:
        return "Idle"
    return "Off"


def _get_sd_info():
    if sd and sd.is_mounted:
        info = sd.free_space()
        if info:
            return "{:.0f}MB free".format(info[0] / 1_048_576)
        return "Mounted"
    return "No card"


def _get_uptime():
    secs = time.ticks_diff(time.ticks_ms(), _boot_ticks) // 1000
    mins, s = divmod(secs, 60)
    hrs, m = divmod(mins, 60)
    if hrs > 0:
        return "{}h{}m".format(hrs, m)
    return "{}m{}s".format(m, s)


def _get_free_ram():
    gc.collect()
    free = gc.mem_free()
    if free > 1_048_576:
        return "{:.1f}MB".format(free / 1_048_576)
    return "{:.0f}KB".format(free / 1024)


# --- Keys menu ---

def _build_keys_menu():
    from menu_ui import MenuItem
    items = [
        MenuItem("List Keys", "sub", _build_key_list),
        MenuItem("Add Key", "info", lambda: "via BLE"),
        MenuItem("< Back", "cb", None),
    ]
    return ("Keys", items)


def _build_key_list():
    from menu_ui import MenuItem
    items = []
    if key_store:
        names = key_store.list_keys()
        for name in names:
            items.append(MenuItem(name, "sub", lambda n=name: _build_key_detail(n)))
    if not items:
        items.append(MenuItem("(no keys)", "info", None))
    items.append(MenuItem("< Back", "cb", None))
    gc.collect()
    return ("Keys", items)


def _build_key_detail(name):
    from menu_ui import MenuItem
    items = [
        MenuItem("View", "cb", lambda: _view_key(name)),
        MenuItem("Send BLE", "cb", lambda: _send_key_ble(name)),
        MenuItem("Delete", "cb", lambda: _delete_key(name)),
        MenuItem("< Back", "cb", None),
    ]
    return (name[:18], items)


def _view_key(name):
    """Show first/last chars of key value on display (masked)."""
    if key_store:
        k = key_store.get_key(name)
        if k and k.get("value"):
            v = k["value"]
            if len(v) > 8:
                masked = v[:4] + "..." + v[-4:]
            else:
                masked = v
            _log("Key: " + masked)


def _send_key_ble(name):
    """Send key value over BLE to connected device."""
    if key_store and server:
        k = key_store.get_key(name)
        if k and k.get("value"):
            try:
                server.send(k["value"])
                _log("Sent: " + name)
            except Exception as e:
                _log("Send fail: " + str(e)[:10])
        else:
            _log("Key not found")
    else:
        _log("No BLE/keys")


def _delete_key(name):
    """Delete key from SD card."""
    if key_store:
        if key_store.delete_key(name):
            _log("Deleted: " + name)
        else:
            _log("Not found: " + name)
    # Pop back to key list
    if menu and menu.is_active:
        menu._pop()


# --- Settings menu ---

def _build_settings_menu():
    from menu_ui import MenuItem
    items = [
        MenuItem("LED Bright", "cycle", {
            "values": ["Off", "Low", "Med", "High"],
            "idx": 1,  # Default: Low (matches 0.3 init)
            "cb": _set_led_brightness,
        }),
        MenuItem("BLE Name", "info", lambda: "KeyMaster"),
        MenuItem("WiFi", "info", lambda: "N/A"),
        MenuItem("< Back", "cb", None),
    ]
    return ("Settings", items)


def _set_led_brightness(value):
    if led:
        levels = {"Off": 0.0, "Low": 0.1, "Med": 0.3, "High": 1.0}
        led.brightness = levels.get(value, 0.3)
        # Show current color at new brightness
        if server and hasattr(server, '_connection') and server._connection:
            led.status_connected()
        else:
            led.status_advertising()


# --- OTA menu ---

def _build_ota_menu():
    from menu_ui import MenuItem
    items = [
        MenuItem("Pull Update", "cb", _do_ota_update),
        MenuItem("Cancel", "cb", None),
    ]
    return ("OTA Update?", items)


def _do_ota_update():
    """Trigger ugit pull. This will restart the device on success."""
    # Close menu first, restore display for status messages
    if menu:
        menu.close()
    if display:
        display.restore_status_screen("advertising")
        display.log("OTA: connecting...")
    _restore_button_callbacks()
    try:
        import ugit
        ugit.wificonnect()
        if display:
            display.log("OTA: pulling...")
        ugit.pull_all()
        if display:
            display.log("OTA: done! Reset...")
        import machine
        machine.reset()
    except ImportError:
        _log("OTA: ugit not found")
    except Exception as e:
        _log("OTA fail: " + str(e)[:12])


# --- About ---

def _show_about():
    """Show version info, then close menu."""
    if menu:
        menu.close()
    _restore_button_callbacks()
    if display:
        display.restore_status_screen(
            "connected" if _is_ble_connected() else "advertising"
        )
        display.log("KeyMaster v0.3.0")
        display.log("ESP32-S3-LCD-1.47")
        display.log("github.com/")
        display.log("  turfptax/")
        display.log("  esp32-keymaster")


def _is_ble_connected():
    return server and hasattr(server, '_connection') and server._connection


# ---------------------------------------------------------------
# Button handlers + menu state switching
# ---------------------------------------------------------------

_orig_on_press = None
_orig_on_long_press = None


def _on_button_press(duration_ms):
    """Short press -- STATUS mode: show device info in log."""
    print("Button short press: {}ms".format(duration_ms))
    _log("BTN: short {}ms".format(duration_ms))
    if sd and sd.is_mounted:
        _log("SD: mounted")
    else:
        _log("SD: not available")


def _on_button_long_press(duration_ms):
    """Long press -- STATUS mode: open menu."""
    print("Button long press: {}ms".format(duration_ms))
    if menu and display:
        _enter_menu()
    else:
        _log("Menu not available")


def _enter_menu():
    """Switch from STATUS mode to MENU mode."""
    global _orig_on_press, _orig_on_long_press
    if not menu or not button:
        return
    # Save original callbacks
    _orig_on_press = button.on_press
    _orig_on_long_press = button.on_long_press
    # Swap to menu navigation callbacks
    button.on_press = lambda d: _menu_short_press(d)
    button.on_long_press = lambda d: _menu_long_press(d)
    # Build and open the root menu
    items = _build_main_menu()
    menu.open("KeyMaster", items)


def _menu_short_press(duration_ms):
    """MENU mode: advance cursor."""
    if menu and menu.is_active:
        menu.on_short_press()


def _menu_long_press(duration_ms):
    """MENU mode: select item."""
    if menu and menu.is_active:
        menu.on_long_press()
        # Check if menu was closed by the action (< Back from root, About, OTA)
        if not menu.is_active:
            _exit_menu()


def _exit_menu():
    """Return from MENU mode to STATUS mode."""
    _restore_button_callbacks()
    if display:
        state = "connected" if _is_ble_connected() else "advertising"
        display.restore_status_screen(state)


def _restore_button_callbacks():
    """Restore original button callbacks."""
    global _orig_on_press, _orig_on_long_press
    if button and _orig_on_press:
        button.on_press = _orig_on_press
        button.on_long_press = _orig_on_long_press
        _orig_on_press = None
        _orig_on_long_press = None


# ---------------------------------------------------------------
# BLE / Bridge callbacks
# ---------------------------------------------------------------

def on_bridge_activity(direction, message):
    """Called by SerialBridge on data flow. Updates display and LED."""
    if direction == "serial_in":
        _log("S>B: " + message[:16])
        if led:
            led.set_color(0, 200, 200)  # Teal for serial->BLE
    elif direction == "ble_in":
        _log("B>S: " + message[:16])
        if led:
            led.status_rx()


def on_status(event, detail=""):
    """Called on BLE state transitions. Updates display and LED."""
    if display:
        if event == "advertising":
            display.show_advertising()
        elif event == "connected":
            display.show_connected(detail)
        elif event == "disconnected":
            display.show_disconnected()
        elif event == "error":
            display.log("ERR: " + detail)

    if led:
        if event == "advertising":
            led.status_advertising()
        elif event == "connected":
            led.status_connected()
        elif event == "disconnected":
            led.status_disconnected()
        elif event == "error":
            led.status_error()


# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------

async def main():
    global server, bridge

    print("=" * 44)
    print("  Cortex Link  |  BLE Bridge  |  v0.3.0")
    print("=" * 44)
    print()
    print("This is a Cortex wearable AI memory dongle.")
    print("It bridges USB serial to a Pi Zero 2 W over")
    print("Bluetooth Low Energy (BLE).")
    print()
    print("-- Quick Setup (any computer) --")
    print()
    print("  pip install git+https://github.com/")
    print("    turfptax/cortex.git")
    print()
    print("  python -m cortex_mcp setup")
    print("  python -m cortex_mcp setup --target claude-desktop")
    print("  python -m cortex_mcp ping")
    print()
    print("Docs: https://github.com/turfptax/cortex")
    print()
    print("-- Hardware Controls --")
    print()
    print("  Short press BOOT = device info")
    print("  Long press BOOT  = open menu")
    print()
    print("-- Status --")
    print()
    if sd and sd.is_mounted:
        print("  SD card: mounted at", sd.mount_point)
    else:
        print("  SD card: not available")
    if key_store:
        keys = key_store.list_keys()
        print("  Keys stored:", len(keys))
    if led:
        print("  NeoPixel LED: active")
    if menu:
        print("  Menu system: ready")
    if button:
        print("  BOOT button: monitored")
    print()
    print("Bridge: USB Serial <-> BLE transparent pipe")
    print("  Newline-delimited, max 512 bytes/msg")
    print("  Send CMD:ping to test round trip")
    print()

    _log("Starting BLE...")

    # Create BLE server (on_receive wired to bridge below)
    server = BLEServer(
        device_name="KeyMaster",
        on_receive=None,
        on_status=on_status,
    )

    # Create serial bridge and wire as BLE receive handler
    from serial_bridge import SerialBridge
    bridge = SerialBridge(
        ble_server=server,
        on_activity=on_bridge_activity,
    )
    server._on_receive = bridge.on_ble_receive

    _log("BLE + Bridge ready")

    # Gather all async tasks
    tasks = [server.run(), bridge.run()]
    if button:
        tasks.append(button.monitor())

    await asyncio.gather(*tasks)


asyncio.run(main())
