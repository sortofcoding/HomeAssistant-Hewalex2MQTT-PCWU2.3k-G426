from binascii import hexlify, unhexlify
import logging
import time

from ..crc import *

# Based on work by krzysztof1111111111
# https://www.elektroda.pl/rtvforum/topic3499254.html


class BaseDevice:
    def __init__(self, conHardId, conSoftId, devHardId, devSoftId, onMessage=None):
        self.logger = logging.getLogger(__name__)
        self.conHardId = conHardId  # G-426 controller - physical address
        self.conSoftId = conSoftId  # G-426 controller - logical address
        self.devHardId = devHardId  # Hewalex device - physical address
        self.devSoftId = devSoftId  # Hewalex device - logical address
        self.onMessage = onMessage  # Callback - onMessage(obj, h, sh, m)

    def parseHardHeader(self, m):
        if len(m) < 8:
            raise Exception("Too short message")
        calcCrc = crc8(m[:7])
        return {
            "StartByte": m[0],
            "To": m[1],
            "From": m[2],
            "ConstBytes": (m[5] << 16) | (m[4] << 8) | m[3],
            "Payload": m[6],
            "CRC8": m[7],
            "CalcCRC8": calcCrc
        }

    def validateHardHeader(self, h):
        if h["StartByte"] != 0x69:
            raise Exception("Invalid Start Byte")
        if h["CRC8"] != h["CalcCRC8"]:
            raise Exception("Invalid Hard CRC8")
        if h["ConstBytes"] != 0x84:
            raise Exception("Invalid Const Bytes")
        if h["From"] != self.conHardId and h["From"] != self.devHardId:
            raise Exception("Invalid From Hard Address: " + str(h["From"]))
        if h["To"] != self.conHardId and h["To"] != self.devHardId:
            raise Exception("Invalid To Hard Address: " + str(h["To"]))
        if h["To"] == h["From"]:
            raise Exception("From and To Hard Address Equal")

    def getWord(self, w):
        return (w[1] << 8) | w[0]

    def getWordReverse(self, w):
        return (w[0] << 8) | w[1]

    def getDWord(self, w):
        return (w[3] << 24) | (w[2] << 16) | (w[1] << 8) | w[0]

    def parseSoftHeader(self, h, m):
        if len(m) != h["Payload"]:
            raise Exception("Invalid soft message len")
        if len(m) < 12:
            raise Exception("Too short soft message")
        calcCrc = crc16(m[:h["Payload"]-2])
        return {
            "To": self.getWord(m[0:]),
            "From": self.getWord(m[2:]),
            "FNC": m[4],
            "ConstByte": self.getWord(m[5:]),
            "RegLen": m[7],
            "RegStart": self.getWord(m[8:]),
            "RestMessage": m[10:h["Payload"]-2],
            "CRC16": self.getWordReverse(m[h["Payload"]-2:]),
            "CalcCRC16": calcCrc
        }

    def validateSoftHeader(self, h, sh):
        if sh["CRC16"] != sh["CalcCRC16"]:
            raise Exception("Invalid Soft CRC16")
        if sh["ConstByte"] != 0x80:
            raise Exception("Invalid Const Soft Byte 0x80")
        if (h["From"] == self.conHardId and sh["From"] != self.conSoftId) or (h["From"] == self.devHardId and sh["From"] != self.devSoftId):
            raise Exception("Invalid From Address")
        if (h["To"] == self.conHardId and sh["To"] != self.conSoftId) or (h["To"] == self.devHardId and sh["To"] != self.devSoftId):
            raise Exception("Invalid To Address")

    def getTemp(self, w, divisor):
        w = self.getWord(w)
        if w & 0x8000:
            w = w - 0x10000
        return w / divisor

    def parseBitMask(self, val, names, ret):
        for name in names:
            if name is not None:
                ret[name] = bool(val & 1)
            val = val >> 1

    def parseRegisters(self, m, regstart, reglen, unknown=False):
        ret = {}

        skip = 0
        for regnum in range(regstart, regstart + reglen, 2):
            if skip > 0:
                skip = skip - 1
                continue
            reg = self.registers.get(regnum, None)
            adr = regnum - regstart
            if reg:
                val = None
                if reg['type'] == 'date':
                    val = "20{:02d}-{:02d}-{:02d}".format(m[adr], m[adr+1], m[adr+2])
                    skip = 1
                elif reg['type'] == 'time':
                    val = "{:02d}:{:02d}:{:02d}".format(m[adr], m[adr+1], m[adr+2])
                    skip = 1
                elif reg['type'] == 'word':
                    val = self.getWord(m[adr:])
                elif reg['type'] == 'rwrd':
                    val = self.getWordReverse(m[adr:])
                elif reg['type'] == 'dwrd':
                    val = self.getDWord(m[adr:])
                    skip = 1
                elif reg['type'] == 'temp':
                    val = self.getTemp(m[adr:], 1.0)
                elif reg['type'] == 'te10':
                    val = self.getTemp(m[adr:], 10.0)
                elif reg['type'] == 'fl10':
                    val = self.getWord(m[adr:]) / 10.0
                elif reg['type'] == 'f100':
                    val = self.getWord(m[adr:]) / 100.0
                elif reg['type'] == 'bool':
                    val = bool(self.getWord(m[adr:]))
                elif reg['type'] == 'mask':
                    self.parseBitMask(self.getWord(m[adr:]), reg['name'], ret)
                    continue
                elif reg['type'] == 'tprg':
                    val = {}
                    self.parseBitMask(self.getDWord(m[adr:]), range(0, 24), val)
                    skip = 1
                ret[reg['name']] = val
            elif unknown:
                ret["Reg%d" % regnum] = self.getWord(m[adr:])

        return ret

    def printMessage(self, h, sh):
        print({
            'hard': {
                "StartByte": hex(h["StartByte"]),
                "To": h["To"],
                "From": h["From"],
                "ConstBytes": hex(h["ConstBytes"]),
                "Payload": h["Payload"],
                "CRC8": hex(h["CRC8"])
            },
            'soft': {
                "To": sh["To"],
                "From": sh["From"],
                "FNC": hex(sh["FNC"]),
                "ConstByte": hex(sh["ConstByte"]),
                "RegLen": sh["RegLen"],
                "RegStart": str(sh["RegStart"]) + " (" + hex(sh["RegStart"]) + ")",
                "RestMessage": hexlify(sh["RestMessage"]),
                "CRC16": hex(sh["CRC16"])
            }
        })

    def processMessage(self, m, ignoreTooShort):
        h = self.parseHardHeader(m)
        self.validateHardHeader(h)
        ml = h["Payload"]
        if ignoreTooShort and ml + 8 > len(m):
            return m
        sh = self.parseSoftHeader(h, m[8:ml+8])
        self.validateSoftHeader(h, sh)
        if self.onMessage:
            self.onMessage(self, h, sh, m)
        return m[ml+8:]

    def processAllMessages(self, m, returnRemainingBytes=False):
        minLen = 8 if returnRemainingBytes else 0
        prevLen = len(m)
        while prevLen > minLen:
            m = self.processMessage(m, returnRemainingBytes)
            if len(m) == prevLen:
                if returnRemainingBytes:
                    return m
                else:
                    raise Exception("Something wrong")
            prevLen = len(m)
        return m

    # Process all messages in X cycles of device to controller comms
    #
    # 1. The device sends a query to the controller to read 20 registers starting from address 100.
    # 2. The controller responds, it always seems to be the same.
    # 3. The device sends a record of registers starting at address 120. This is the main message
    #    with temperatures. After this message there is a long pause, probably the controller is
    #    not too fast to write it to memory. The exact number of records depends on model/firmware.
    # 4. The controller replies with 0x70 (bytes written successfully).
    # 5. The device requests to read 4 bytes starting from address 252.
    # 6. The controller responds: 10000000 = normal, 08000000 = controller off,
    #    11000000 = user changed a parameter in the menu.
    #
    def eavesDrop(self, ser, numCycles=None):
        # Window size and time (based on 140ms cycle and 360ms wait)
        winSize = 1000
        winTime = 0.4

        # Determine start-of-cycle marker (device requesting 20 registers from address 100)
        s1 = unhexlify('69%02x%02x8400000c' % (self.devHardId, self.conHardId))
        s2 = unhexlify('%02x' % crc8(s1))
        s3 = unhexlify('%02x%02x%02x%02x' % (
            (self.devSoftId & 0xff),
            (self.devSoftId & 0xff00) >> 8,
            (self.conSoftId & 0xff),
            (self.conSoftId & 0xff00) >> 8,
        ))
        s4 = unhexlify('408000146400')
        s = s1 + s2 + s3 + s4

        ser.flushInput()
        cnt = 0
        while (numCycles is None) or (cnt < numCycles):
            cnt += 1
            ser.timeout = 1
            m = ser.read_until(s, winSize)
            ser.timeout = winTime
            m = ser.read(winSize)
            self.processAllMessages(s + m)

    def waitForBusQuiet(self, ser, quiet_window=0.05, max_wait=0.5):
        """Wait for a gap in bus traffic before transmitting, to reduce
        collisions with the G-426 controller's own ongoing communication
        with the heat pump on a shared/eavesdropped RS485 bus.

        Returns True if the bus was quiet for one full window before
        transmitting, False if max_wait was reached without a quiet gap
        (in which case we transmit anyway rather than waiting forever).
        """
        start = time.time()
        ser.timeout = quiet_window
        while time.time() - start < max_wait:
            data = ser.read(100)
            if not data:
                return True  # bus has been quiet for one full window
        return False  # gave up waiting; bus seems continuously busy

    def createReadRegistersMessage(self, start, num):
        header = [0x69, self.devHardId, self.conHardId, 0x84, 0, 0]
        payload = [
            (self.devSoftId & 0xff), ((self.devSoftId >> 8) & 0xff),
            (self.conSoftId & 0xff), ((self.conSoftId >> 8) & 0xff),
            0x40, 0x80, 0, num & 0xff, start & 0xff, (start >> 8) & 0xff
        ]
        calcCrc16 = crc16(payload)
        payload.append((calcCrc16 >> 8) & 0xff)
        payload.append(calcCrc16 & 0xff)
        header.append(len(payload))
        calcCrc8 = crc8(header)
        header.append(calcCrc8)
        return bytearray(header + payload)

    def createWriteRegisterMessage(self, reg, val):
        header = [0x69, self.devHardId, self.conHardId, 0x84, 0, 0]
        payload = [
            (self.devSoftId & 0xff), ((self.devSoftId >> 8) & 0xff),
            (self.conSoftId & 0xff), ((self.conSoftId >> 8) & 0xff),
            0x60, 0x80, 0, 2, reg & 0xff, (reg >> 8) & 0xff,
            val & 0xff, (val >> 8) & 0xff
        ]
        calcCrc16 = crc16(payload)
        payload.append((calcCrc16 >> 8) & 0xff)
        payload.append(calcCrc16 & 0xff)
        header.append(len(payload))
        calcCrc8 = crc8(header)
        header.append(calcCrc8)
        return bytearray(header + payload)

    def readRegisters(self, ser, start, num):
        m = self.createReadRegistersMessage(start, num)
        self.logger.debug(f'Reading registers from {start}, count {num}')
        ser.flushInput()
        self.waitForBusQuiet(ser)
        ser.timeout = 0.4
        ser.write(m)
        r = ser.read(1000)
        return self.processAllMessages(r)

    def readStatusRegisters(self, ser):
        self.logger.info('readStatusRegisters')
        start = self.REG_STATUS_START
        return self.readRegisters(ser, start, self.REG_CONFIG_START - start)

    def readConfigRegisters(self, ser):
        start = self.REG_CONFIG_START
        while start < self.REG_MAX_ADR:
            num = min(self.REG_MAX_ADR + 2 - start, self.REG_MAX_NUM)
            self.readRegisters(ser, start, num)
            start = start + num

    def writeRegister(self, ser, reg, val):
        m = self.createWriteRegisterMessage(reg, val)
        self.logger.debug(f'Writing register {reg} = {val}')
        ser.flushInput()
        self.waitForBusQuiet(ser)
        ser.timeout = 0.4
        ser.write(m)
        r = ser.read(1000)
        return self.processAllMessages(r)

    def write(self, ser, registername, val):
        regnum = 0
        for k, v in self.registers.items():
            if v['name'] == registername:
                regnum = k
                break
        reg = self.registers.get(regnum, None)
        if reg:
            val = self.parseRegisterValue(reg, val)
            if val is not None:
                return self.writeRegister(ser, regnum, val)
        return None

    def parseRegisterValue(self, reg, val):
        if val:
            if reg['type'] == 'date':
                val = None
            elif reg['type'] == 'time':
                val = None
            elif reg['type'] == 'word':
                val = int(val)
            elif reg['type'] == 'rwrd':
                val = int(val)
            elif reg['type'] == 'dwrd':
                val = int(val)
            elif reg['type'] == 'temp':
                val = int(val)
            elif reg['type'] == 'te10':
                val = int(val) * 10
            elif reg['type'] == 'fl10':
                val = float(val) * 10
            elif reg['type'] == 'f100':
                val = float(val) * 100
            elif reg['type'] == 'bool':
                if val == 'True' or val == '1':
                    val = 1
                elif val == 'False' or val == '0':
                    val = 0
            elif reg['type'] == 'mask':
                val = None
            elif reg['type'] == 'tprg':
                val = None
            if reg['options'] and val not in reg['options']:
                self.logger.warning(f'Invalid option {val} for register {reg["name"]}')
                val = None
            return val


# Interface to implement in child classes
#########################################

    # Registers are divided in read-only status registers and read/write config registers

    # The lowest readable register always seems to be 100
    REG_MIN_ADR = 100

    # The highest readable register varies per device
    REG_MAX_ADR = 536

    # The number of registers which can be read in one message varies per device
    REG_MAX_NUM = 100

    # Status registers start at 120
    REG_STATUS_START = 120

    # The number of status registers which can be read in one message varies per device
    REG_STATUS_NUM = 104

    # Config registers start at a register which varies per device
    REG_CONFIG_START = 302

    registers = {
#
#        # Status registers
#        120: { 'type': 'date', 'name': 'date' },
#        128: { 'type': 'te10', 'name': 'T1' },
#        ...
#
#        # Config registers
#        XXX: { 'type': 'word', 'name': 'InstallationScheme' },
#        ...
#
    }
