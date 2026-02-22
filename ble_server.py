import gc
import asyncio
import bluetooth
import aioble
from bluetooth import UUID


# Custom UUIDs for the KeyMaster service
_SERVICE_UUID = UUID("a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e50")
_TX_UUID = UUID("a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e51")  # ESP32 -> Phone
_RX_UUID = UUID("a0e1b2c3-d4e5-f6a7-b8c9-0a1b2c3d4e52")  # Phone -> ESP32

_ADV_INTERVAL_US = 250_000  # 250ms


class BLEServer:
    def __init__(self, device_name="KeyMaster", on_receive=None, on_status=None):
        self._name = device_name
        self._on_receive = on_receive
        self._on_status = on_status
        self._connection = None

        # Configure BLE — must activate before setting MTU
        try:
            ble = bluetooth.BLE()
            ble.active(True)
            ble.config(mtu=512)
            ble.config(bond=True)
            ble.config(mitm=False)
            ble.config(io=3)  # 3 = NoInputNoOutput (just works pairing)
            print("BLE: active, MTU=512, bonding configured")
        except Exception as e:
            print("BLE: config error:", e)

        # Register GATT service and characteristics
        service = aioble.Service(_SERVICE_UUID)
        self._tx_char = aioble.Characteristic(
            service, _TX_UUID, read=True, notify=True,
        )
        self._rx_char = aioble.Characteristic(
            service, _RX_UUID, write=True, capture=True,
        )
        aioble.register_services(service)

    @property
    def connected(self):
        return self._connection is not None and self._connection.is_connected()

    def _notify_status(self, event, detail=""):
        """Fire the status callback if registered. Errors are caught to
        prevent display issues from crashing the BLE server."""
        if self._on_status:
            try:
                self._on_status(event, detail)
            except Exception as e:
                print("BLE: status callback error:", e)

    def send(self, data):
        """Send a string to the connected device via TX notification."""
        if not self.connected:
            print("BLE: not connected, cannot send")
            return
        self._tx_char.write(data.encode("utf-8"), send_update=True)

    def send_raw(self, data_bytes):
        """Send raw bytes via TX notification (no encoding).
        Used by SerialBridge for pre-encoded / chunked data."""
        if not self.connected:
            return
        self._tx_char.write(data_bytes, send_update=True)

    async def run(self):
        """Start the BLE server. Runs forever (advertise + receive loop)."""
        await asyncio.gather(
            self._advertise_task(),
            self._rx_task(),
        )

    async def _advertise_task(self):
        while True:
            print("BLE: advertising as '{}'...".format(self._name))
            self._notify_status("advertising")
            try:
                connection = await aioble.advertise(
                    _ADV_INTERVAL_US,
                    name=self._name,
                    services=[_SERVICE_UUID],
                    connectable=True,
                )
                self._connection = connection
                print("BLE: connected to", connection.device)

                # Negotiate larger MTU for Cortex protocol messages
                try:
                    await connection.exchange_mtu(512)
                    print("BLE: MTU exchanged")
                except Exception as e:
                    print("BLE: MTU exchange skipped:", e)

                self._notify_status("connected", str(connection.device))

                # Poll for disconnection -- more reliable than
                # await connection.disconnected() which can miss events
                while connection.is_connected():
                    await asyncio.sleep_ms(500)

            except aioble.DeviceDisconnectedError:
                pass
            except asyncio.CancelledError:
                raise
            except OSError as e:
                print("BLE: OS error:", e)
                self._notify_status("error", str(e))
                await asyncio.sleep_ms(1000)
            except Exception as e:
                print("BLE: advertise error:", e)
                self._notify_status("error", str(e))
                await asyncio.sleep_ms(1000)

            self._connection = None
            print("BLE: disconnected")
            self._notify_status("disconnected")
            gc.collect()

    async def _rx_task(self):
        rx_buf = bytearray()
        while True:
            try:
                connection, data = await self._rx_char.written(timeout_ms=1000)
            except asyncio.TimeoutError:
                await asyncio.sleep_ms(50)
                continue
            except aioble.DeviceDisconnectedError:
                rx_buf = bytearray()
                await asyncio.sleep_ms(100)
                continue

            rx_buf.extend(data)

            # Process complete newline-delimited messages
            while b"\n" in rx_buf:
                idx = rx_buf.index(b"\n")
                try:
                    message = bytes(rx_buf[:idx]).decode("utf-8")
                except UnicodeError:
                    message = str(bytes(rx_buf[:idx]))
                rx_buf = rx_buf[idx + 1:]

                self._notify_status("rx", message)

                if self._on_receive:
                    self._on_receive(self, message, connection)

            # Safety: discard buffer if too large without newline
            if len(rx_buf) > 4096:
                rx_buf = bytearray()
