import appdaemon.plugins.hass.hassapi as hass
import configparser
import serial
from hewalex_geco.devices import PCWU
import paho.mqtt.client as mqtt
import time
import traceback
import datetime
import random
import threading

VERSION = "2026-03-20h"
# HeatPumpEnabled commands via HA listen_state + input_boolean bridge
# paho on_message unreliable in AppDaemon 4.5.13 for non-retained messages

SOFT_ERRORS = ("Invalid soft message len", "Invalid Const Bytes")

BLOCKED_COMMAND_REGISTERS = frozenset()

# Human-readable translation of WaitingStatus values
# Published as WaitingStatusText alongside the raw WaitingStatus value
WAITING_STATUS_TEXT = {
    "0": "Running",
    "1": "Ext Off",    # disabled via physical switch
    "2": "MQTT Off",   # disabled via MQTT command
    "16": "Defrost",
}


class Hewalex2MQTT(hass.Hass):
    """Read Hewalex heat pump via RS485, publish to MQTT, alert HA on failure."""

    def initialize(self):
        self.log(f"Starting Hewalex 2 MQTT - version {VERSION}")

        self.MessageCache = {}
        self.ser_lock = threading.Lock()
        self.mqtt_lock = threading.Lock()
        self._last_read_count = 0
        self._last_new_count = 0

        self.last_mqtt_restart = 0
        self.last_success = time.time()
        self.offline_reported = False
        self.writing_active = False
        self.last_write_time = 0
        self.rs485_block_until = 0

        self._last_command = {}  # {reg: (payload, timestamp)}

        self.initConfiguration()

        self.dev = PCWU(1, 1, 2, 2, self.on_message_serial)
        self.start_mqtt()

        # write-queue + worker thread  (ONLY in normal mode)
        if not self._read_only:
            self.write_queue = {}
            self.write_lock = threading.Lock()
            self.write_thread_stop = threading.Event()
            self.write_thread = threading.Thread(target=self.write_worker, daemon=True)
            self.write_thread.start()

            # HeatPumpEnabled via listen_state (reliable) instead of paho on_message
            self.listen_state(
                self.on_switch_toggle,
                "input_boolean.warmtepomp_command",
            )
            self.log("listen_state registered on input_boolean.warmtepomp_command")
        else:
            self.log("Read-only mode: write worker and HA switch listener disabled")

        start_poll = self.datetime() + datetime.timedelta(seconds=5)
        start_watchdog = self.datetime() + datetime.timedelta(seconds=60)
        start_config = self.datetime() + datetime.timedelta(seconds=90)

        # Periodic polling (ONLY in normal mode)
        if not self._read_only:
            self.poll_handle = self.run_every(self.readPCWU_cb, start_poll, 60)
            self.config_refresh_handle = self.run_every(
                self.readPcwuConfig_cb, start_config, 600
            )
            self.log("Config-read interval: 10 min")
        else:
            self.log("Read-only mode: periodic status/config polling disabled")

        # Watchdog runs in both modes (useful to know if the bus is alive)
        self.watchdog_handle = self.run_every(self.watchdog_cb, start_watchdog, 60)

        # Diagnostic dump runs in both modes if requested
        if self._diagnostic_dump:
            self.run_in(self.dumpAllRegisters_cb, 15)

    def terminate(self):
        if hasattr(self, "write_thread_stop"):
            try:
                self.write_thread_stop.set()
            except Exception:
                pass
        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    # ---------------------------------------------------------------
    # Configuration
    # ---------------------------------------------------------------
    def initConfiguration(self):
        cfg = configparser.ConfigParser()
        cfg.read("/config/apps/hewalex2mqttconfig.ini")

        self._MQTT_ip = cfg["MQTT"]["MQTT_ip"]
        self._MQTT_port = cfg.getint("MQTT", "MQTT_port")
        self._MQTT_auth = cfg.getboolean("MQTT", "MQTT_authentication")
        self._MQTT_user = cfg["MQTT"]["MQTT_user"]
        self._MQTT_pass = cfg["MQTT"]["MQTT_pass"]

        self._addr = cfg["Pcwu"]["Device_Pcwu_Address"]
        self._port = cfg["Pcwu"]["Device_Pcwu_Port"]
        self._topic = cfg["Pcwu"]["Device_Pcwu_MqttTopic"]
        self._enabled = cfg.getboolean("Pcwu", "Device_Pcwu_Enabled")

        # Clean boolean reads with fallbacks
        self._debug = cfg.getboolean("Pcwu", "DebugLogging", fallback=False)
        self._diagnostic_dump = cfg.getboolean("Pcwu", "DiagnosticDump", fallback=False)
        self._read_only = cfg.getboolean("Pcwu", "ReadOnlyMode", fallback=False)

        if self._read_only:
            self.log("=== READ-ONLY MODE ACTIVE ===")

    def dlog(self, msg):
        if getattr(self, "_debug", False):
            self.log(msg, level="DEBUG")

    # ---------------------------------------------------------------
    # MQTT
    # ---------------------------------------------------------------
    def start_mqtt(self):
        with self.mqtt_lock:
            now = time.time()
            if now - self.last_mqtt_restart < 30:
                self.dlog("MQTT reconnect suppressed (too soon)")
                return
            self.last_mqtt_restart = now

        try:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass

            self.client = mqtt.Client(clean_session=True)
            if self._MQTT_auth:
                self.client.username_pw_set(self._MQTT_user, self._MQTT_pass)

            self.client.on_connect = self.on_connect
            self.client.on_disconnect = self.on_disconnect
            self.client.on_message = self.on_message

            self.client.connect_async(self._MQTT_ip, self._MQTT_port, keepalive=60)
            self.client.loop_start()

            self.log("MQTT connecting (async)")
        except Exception as e:
            self.log(f"MQTT connect failed: {e}")
            self.log(traceback.format_exc(), level="DEBUG")

    def on_connect(self, client, userdata, flags, rc):
        self.log(f"MQTT: on_connect rc={rc} - subscribing")
        if not self._read_only:
            client.subscribe(self._topic + "/Command/#", qos=1)
        else:
            self.log("Read-only mode: skipping Command/# subscription")

    def on_disconnect(self, client, userdata, rc):
        if rc != 0:
            self.log(f"MQTT disconnected ({rc}), retrying in 5s")
            t = threading.Timer(5.0, self.start_mqtt)
            t.daemon = True
            t.start()

    # ---------------------------------------------------------------
    # MQTT callback (for config registers; HeatPumpEnabled via listen_state)
    # ---------------------------------------------------------------
    def on_message(self, client, userdata, msg):
        try:
            if self._read_only:
                return   # silently ignore all command messages
            
            payload = msg.payload.decode()
            topic = msg.topic.split("/")
            if len(topic) == 3 and topic[0] == self._topic and topic[1] == "Command":
                reg = topic[2]

                # HeatPumpEnabled is handled via listen_state - skip here
                if reg == "HeatPumpEnabled":
                    self.dlog(f"on_message: HeatPumpEnabled skipped (handled via listen_state)")
                    return

                # Other registers: deduplicate within 10s
                now = time.time()
                last_payload, last_ts = self._last_command.get(reg, (None, 0))
                if last_payload == payload and (now - last_ts) < 10:
                    self.dlog(f"Command {reg} -> {payload} ignored (duplicate within 10s)")
                    return
                self._last_command[reg] = (payload, now)

                if reg in BLOCKED_COMMAND_REGISTERS:
                    self.dlog(f"Command {reg} ignored (read-only register)")
                    return

                self.log(f"Command {reg} -> {payload}")
                with self.write_lock:
                    self.write_queue[reg] = payload
        except Exception as e:
            self.log(f"MQTT message error: {e}")
            self.log(traceback.format_exc(), level="DEBUG")

    # ---------------------------------------------------------------
    # listen_state handler - HeatPumpEnabled via input_boolean
    # ---------------------------------------------------------------
    def on_switch_toggle(self, entity, attribute, old, new, kwargs):
        if self._read_only:
            return
            
        if new in ("on", "off"):
            payload = "True" if new == "on" else "False"
            self.log(f"Switch toggle via listen_state: HeatPumpEnabled -> {payload}")
            with self.write_lock:
                self.write_queue["HeatPumpEnabled"] = payload

    # ---------------------------------------------------------------
    # Write worker
    # ---------------------------------------------------------------
    def write_worker(self):
        while not self.write_thread_stop.is_set():
            try:
                if self._read_only:
                    time.sleep(1.0)
                    continue
                item = None
                with self.write_lock:
                    if self.write_queue:
                        reg, payload = self.write_queue.popitem()
                        item = (reg, payload)

                if not item:
                    time.sleep(0.1)
                    continue

                reg, payload = item
                self.writePcwuConfig(reg, payload)
                time.sleep(0.5)

                with self.write_lock:
                    queue_empty = not bool(self.write_queue)
                if queue_empty:
                    self.readPcwuConfig()

            except Exception as e:
                self.log(f"Write worker error: {e}")
                self.log(traceback.format_exc(), level="DEBUG")
                time.sleep(1.0)

    # ---------------------------------------------------------------
    # Periodic config read callback
    # ---------------------------------------------------------------
    def readPcwuConfig_cb(self, kwargs):
        try:
            if self.writing_active:
                self.dlog("Config-read skipped: write active")
                return
            if not self._rs485_available():
                self.dlog("Config-read skipped: RS485 temporarily blocked")
                return
            self.readPcwuConfig()
        except Exception as e:
            self.log(f"Deferred config read error: {e}")
            self.log(traceback.format_exc(), level="DEBUG")

    # ---------------------------------------------------------------
    # Serial callback
    # ---------------------------------------------------------------
    def on_message_serial(self, obj, h, sh, m):
        try:
    #        Uncommend for debugging and testing:
    #        self.log(f"Serial msg received: FNC={sh.get('FNC')} h={h} sh={sh}")
            if sh["FNC"] == 0x50:
                mp = obj.parseRegisters(
                    sh["RestMessage"], sh["RegStart"], sh["RegLen"]
                )
                new_values = 0
                for reg, val in mp.items():
                    if isinstance(val, dict):
                        continue
                    key = f"{self._topic}/{reg}"
                    val_str = str(val)
                    if self.MessageCache.get(key) != val_str:
                        self.MessageCache[key] = val_str
                        new_values += 1
                        self.client.publish(key, val_str, retain=True)

                        # Publish human-readable text alongside WaitingStatus
                        if reg == "WaitingStatus":
                            text = WAITING_STATUS_TEXT.get(val_str, f"Unknown ({val_str})")
                            text_key = f"{self._topic}/WaitingStatusText"
                            self.client.publish(text_key, text, retain=True)
                            self.dlog(f"WaitingStatus {val_str} -> {text}")

                self.last_success = time.time()
                self.offline_reported = False
                self.set_state("sensor.hewalex_status", state="online")
                self._last_read_count = getattr(self, "_last_read_count", 0) + len(mp)
                self._last_new_count = getattr(self, "_last_new_count", 0) + new_values

        except Exception as e:
            self.log(f"Serial parse error: {e}")
            self.log(traceback.format_exc(), level="DEBUG")

    # ---------------------------------------------------------------
    # Polling & watchdog
    # ---------------------------------------------------------------
    def readPCWU_cb(self, kwargs):
        if self.writing_active:
            self.dlog("Read skipped: write active")
            return
        if time.time() - self.last_write_time < 10:
            self.dlog("Read skipped: backoff after write")
            return
        if not self._rs485_available():
            self.dlog("Read skipped: RS485 temporarily blocked")
            return
        time.sleep(random.uniform(0.0, 0.3))
        try:
            self.readPCWU()
        except Exception as e:
            self.log(f"Polling error: {e}")
            self.log(traceback.format_exc(), level="DEBUG")

    def watchdog_cb(self, kwargs):
        elapsed = time.time() - self.last_success
        if elapsed > 300 and not self.offline_reported:
            self.offline_reported = True
            self.set_state(
                "sensor.hewalex_status",
                state="offline",
                attributes={"message": "RS485 unreachable >5 min"},
            )
            self.log("RS485 gateway unreachable >5 min - alerting HA")

    # ---------------------------------------------------------------
    # RS485 helpers
    # ---------------------------------------------------------------
    def _rs485_available(self):
        return time.time() >= getattr(self, "rs485_block_until", 0)

    def _handle_rs485_hard_error(self, msg):
        self.rs485_block_until = time.time() + 60
        self.log(f"RS485 blocked for 60s due to error: {msg}")

    # ---------------------------------------------------------------
    # Read/write with lock
    # ---------------------------------------------------------------
    def readPCWU(self):
        if not self._rs485_available():
            self.dlog("readPCWU skipped: RS485 temporarily blocked")
            return
        self._last_read_count = 0
        self._last_new_count = 0
        start = time.perf_counter()
        try:
            with self.ser_lock:
                with serial.serial_for_url(
                    f"socket://{self._addr}:{self._port}", timeout=5
                ) as ser:
                    self.dev.readStatusRegisters(ser)
                time.sleep(0.25)
            total = getattr(self, "_last_read_count", 0)
            new = getattr(self, "_last_new_count", 0)
            elapsed = time.perf_counter() - start
            self.log(f"Read OK - {total} registers, {new} new - {elapsed:.2f}s")
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in SOFT_ERRORS):
                self.dlog(f"RS485 read error (soft): {msg}")
                return
            self.log(f"RS485 read error: {msg}")
            self.log(traceback.format_exc(), level="DEBUG")
            if any(x in msg for x in ["disconnected", "Broken", "reset", "timeout", "filedescriptor out of range", "Could not open port"]):
                self._handle_rs485_hard_error(msg)
                t = threading.Timer(5.0, self.start_mqtt)
                t.daemon = True
                t.start()

    def writePcwuConfig(self, reg, payload):
        """Write register to Hewalex with lock, rest pause and clear logging."""
        if self._read_only:
            self.log(f"Write {reg}={payload} blocked: read-only mode")
            return
        if not self._rs485_available():
            self.log(f"Write {reg}={payload} skipped: RS485 temporarily blocked")
            return
        self.writing_active = True
        ok = False
        try:
            with self.ser_lock:
                with serial.serial_for_url(
                    f"socket://{self._addr}:{self._port}",
                    timeout=5,
                    write_timeout=5,
                    inter_byte_timeout=0.5,
                    rtscts=False,
                    dsrdtr=False,
                ) as ser:
                    dev = PCWU(1, 1, 2, 2, self.on_message_serial)
                    dev.write(ser, reg, payload)
                ok = True
                time.sleep(0.25)
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in SOFT_ERRORS):
                self.dlog(f"Write (soft error) ignored: {msg}")
            else:
                self.log(f"Write error: {msg}")
                self.log(traceback.format_exc(), level="DEBUG")
            if any(x in msg for x in ["disconnected", "Broken", "reset", "timeout", "filedescriptor out of range", "Could not open port"]):
                self._handle_rs485_hard_error(msg)
                t = threading.Timer(5.0, self.start_mqtt)
                t.daemon = True
                t.start()
        finally:
            self.writing_active = False
            self.last_write_time = time.time()
            self.log(f"Write {reg}={payload} - {'success' if ok else 'failed'}")

    def readPcwuConfig(self):
        if not self._rs485_available():
            self.dlog("readPcwuConfig skipped: RS485 temporarily blocked")
            return
        self._last_read_count = 0
        self._last_new_count = 0
        start = time.perf_counter()
        try:
            with self.ser_lock:
                with serial.serial_for_url(
                    f"socket://{self._addr}:{self._port}", timeout=5
                ) as ser:
                    dev = PCWU(1, 1, 2, 2, self.on_message_serial)
                    dev.readConfigRegisters(ser)
                time.sleep(0.25)
            total = getattr(self, "_last_read_count", 0)
            new = getattr(self, "_last_new_count", 0)
            elapsed = time.perf_counter() - start
            self.log(f"Config-read done - {total} registers, {new} new - {elapsed:.2f}s")
        except Exception as e:
            msg = str(e)
            if any(x in msg for x in SOFT_ERRORS):
                self.dlog(f"Config read (soft error) ignored: {msg}")
                return
            self.log(f"Config read error: {msg}")
            self.log(traceback.format_exc(), level="DEBUG")
            if any(x in msg for x in ["disconnected", "Broken", "reset", "timeout", "filedescriptor out of range", "Could not open port"]):
                self._handle_rs485_hard_error(msg)
    
    def dumpAllRegisters_cb(self, kwargs):
        self.log("=== Starting full raw register dump (100-536) ===")
        self._diag_dump = {}
        try:
            with self.ser_lock:
                with serial.serial_for_url(
                    f"socket://{self._addr}:{self._port}", timeout=5
                ) as ser:
                    dev = PCWU(1, 1, 2, 2, self.on_message_diagnostic)
                    start = dev.REG_MIN_ADR
                    while start < dev.REG_MAX_ADR:
                        num = min(dev.REG_MAX_ADR - start, dev.REG_MAX_NUM)
                        try:
                            dev.readRegisters(ser, start, num)
                        except Exception as e:
                            self.log(f"Dump chunk {start}-{start+num} failed: {e}")
                        time.sleep(0.3)
                        start += num
                time.sleep(0.25)
        except Exception as e:
            self.log(f"Diagnostic dump connection error: {e}")
            self.log(traceback.format_exc(), level="DEBUG")
            return

        self.log(f"=== Raw register dump complete - {len(self._diag_dump)} values found ===")
        for regnum in sorted(self._diag_dump.keys()):
            raw_unsigned, name = self._diag_dump[regnum]
            signed = raw_unsigned - 0x10000 if raw_unsigned & 0x8000 else raw_unsigned
            label = f" -- NAMED: {name}" if name else ""
            self.log(
                f"Reg{regnum}: raw={raw_unsigned} signed={signed} "
                f"(if /10: {signed/10:.1f}){label}"
            )

    def on_message_diagnostic(self, obj, h, sh, m):
        try:
            if sh["FNC"] != 0x50:
                return
            regstart = sh["RegStart"]
            reglen = sh["RegLen"]
            m2 = sh["RestMessage"]
            for adr in range(0, min(reglen, len(m2)) - 1, 2):
                regnum = regstart + adr
                raw_word = m2[adr] | (m2[adr + 1] << 8)
                reg_def = obj.registers.get(regnum, None)
                name = reg_def["name"] if reg_def else None
                self._diag_dump[regnum] = (raw_word, name)
        except Exception as e:
            self.log(f"Diagnostic parse error: {e}")
