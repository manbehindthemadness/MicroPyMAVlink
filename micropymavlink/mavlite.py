"""
This file is an attempt to produce a bare-minimum Mavlink UART communication system.

"""
import array
import struct
from micropymavlink.MSGFormats import formats
from micropymavlink.uart import (
    uart_io,
    read_buffer,
    write_buffer,
)


async def decode_payload(message_id: int, payload: list, format_override: [None, str] = None, debug: bool = False):
    """
    Uses the indexed format to decode the contents of an incoming payload.
    """
    if not format_override:
        _format = "<" + formats[message_id]
    else:
        _format = "<" + format_override
    p_len = struct.calcsize(_format)
    if p_len > len(payload):
        diff = p_len - len(payload)
        suffix = [0] * diff
        payload.extend(suffix)
    if debug:
        print('using format:', _format)
        print('payload length:', len(payload), 'payload', *payload)
    payload = struct.unpack(_format, bytes(payload))
    return payload


class X25crc:
    """
    Improved CRC code.
    """
    def __init__(self):
        self.crc = 0xffff

    async def create(self, buf=None):
        """
        Perform CRC creation actions independent of class init.
        """
        self.crc = 0xffff
        if buf is not None:
            if isinstance(buf, str):
                await self.accumulate_str(buf)
            else:
                await self.accumulate(buf)
        return self

    async def accumulate(self, buf):
        """
        Add in rolling byte-chunks.
        """
        accum = self.crc
        for b in buf:
            tmp = b ^ (accum & 0xff)
            tmp = (tmp ^ (tmp << 4)) & 0xFF
            accum = (accum >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)
        self.crc = accum
        return self

    async def accumulate_str(self, buf: [str, bytes]):
        """
        Add in rolling byte-chunks.
        """
        try:
            bytes_array = array.array('B', list(buf))
        except (AttributeError, TypeError):
            bytes_array = array.array('B', list(buf.encode()))
        except (AttributeError, TypeError):
            raise ValueError
        await self.accumulate(bytes_array)
        return self


class Packet:
    """
    Construct/deconstruct Mavlink packets.
    """
    packet = None

    magic = 0xfd  # Start.
    p_len = 0x00  # 0-255  Payload length.
    incompat = 0x00  # 0-255 Incompatibility Flags: https://mavlink.io/en/guide/serialization.html#incompat_flags
    compat = 0x00  # 0-255 Compatibility Flags: https://mavlink.io/en/guide/serialization.html#compat_flags
    psn = 0x00  # 0-255 Packet sequence number.
    sid = 0x01  # 1-255 System ID (sender).
    cid = 0x01  # 1-255 Component ID (sender): https://mavlink.io/en/messages/common.html#MAV_COMPONENT
    mid = 0x00  # 0-16777215 Message ID (low, middle, high bytes)
    payload: bytes = bytes()  # Payload Max 255 bytes: https://mavlink.io/en/guide/serialization.html#payload
    next_payload: bytes = bytes()  # A place to store payload information that is larger than the allowed size.
    crc = 0xffff  # Checksum (low byte, high byte): https://mavlink.io/en/guide/serialization.html#checksum
    sig = 0x00  # Optional signature: https://mavlink.io/en/guide/message_signing.html

    crc_extra = 0x00

    x25 = X25crc()

    header = bytes()

    def __init__(self):
        self.dummy = None

    async def reset(self):
        """
        Clear all variables to save memory.
        """
        self.p_len = 0x00
        self.incompat = 0x00
        self.compat = 0x00
        self.psn = 0x00
        self.sid = 0x01
        self.cid = 0x01
        self.mid = 0x00
        self.crc = 0xffff
        self.sig = 0x00
        self.payload: bytes = bytes()
        self.crc_extra = 0x00
        self.x25 = X25crc()
        self.header = bytes()
        self.payload = bytes()

        return self

    async def create_header(self):
        """
        Constructs a mavlink 2 header.
        """
        self.header = struct.pack(
            "<BBBBBBBHB",  # noqa
            self.magic,
            self.p_len,
            self.incompat,
            self.compat,
            self.psn,
            self.sid,
            self.cid,
            self.mid & 0xFFFF,
            self.mid >> 16,
        )
        return self

    async def truncate(self):
        """
        Strip nullbytes from payload.
        """
        if not isinstance(self.payload, bytes):
            raise TypeError
        self.p_len = len(self.payload)
        while self.p_len > 1 and self.payload[self.p_len - 1] == 0:
            self.p_len -= 1
        self.payload = self.payload[:self.p_len]
        return self

    async def assemble(self):
        """
        Assembles a packet for transmission.
        """
        await self.create_payload(self.mid, self.payload)
        await self.create_header()
        self.packet = self.header + self.payload
        await self.x25.create(self.packet[1:])
        await self.x25.accumulate_str(struct.pack("B", self.crc_extra))  # noqa
        self.crc = self.x25.crc
        self.packet += struct.pack("<H", self.crc)
        return self

    async def create_payload(self, message_id, payload):
        """
        Pack the user payload according to the specified format.
        """
        _format = formats[message_id]
        if len(payload) != len(_format):
            print('payload malformed: expected', len(_format), 'arguments, received:', len(payload))
            raise ValueError
        _format = "<" + formats[message_id]
        self.payload = struct.pack(_format, *payload)
        await self.truncate()
        self.p_len = len(list(self.payload))
        return self

    async def create_packet(
            self,
            m_id: int,
            payload: [list, tuple],
            c_flags: int = 0x00,
            i_flags: int = 0x00,
            s_id: int = 0x01,
            c_id: int = 0x01,

    ) -> bytes:
        """
        Creates a finished mavlink packet that is ready for transmission.
        """
        await self.reset()
        self.mid = m_id
        self.cid = c_id
        self.sid = s_id
        self.payload = payload
        self.compat = c_flags
        self.incompat = i_flags
        await self.assemble()
        return self.packet

    async def send(self):
        """
        This will transmit our shiny new packet.
        """
        if self.packet:
            write_buffer.append(self.packet)
            await self.reset()
        return self

    @staticmethod
    async def receive():
        """
        Just returns the read buffer.
        """
        return read_buffer


class Heartbeat:
    """
    Send a heartbeat packet.
    """

    packet = Packet()

    def __init__(self):
        self.type = None

    @staticmethod
    async def wait() -> True:
        """
        Waits for a heartbeat.

        TODO: I think we will need to get the system IDs of the flight controller from here.
        """
        beat = False
        while not beat:
            for idx, message in enumerate(read_buffer):
                if message['message_id'] == 0:
                    beat = True
                    del read_buffer[idx]
                    break
        return beat


class Command:
    """
    This will allow us to send commands and read the ACK.
    """

    def __init__(self, s_id: int, c_id: int):
        self.s_id = s_id
        self.c_id = c_id

    async def wait(self, cmd_id: int, debug: bool = False) -> bool:
        """
        This will wait for our command ACK response.

        TODO: We need a timeout here to prevent blocking.

        TODO: I am not 100% sure how the addressing works here; however, it is my assumption that the
                component and system IDs sent in the header identify the packet sender,
                and the target component and system IDs in the payload represent the desired recipient.
        """
        ack = False
        while not ack:
            for idx, message in enumerate(read_buffer):
                if message['message_id'] == 77:
                    if debug:
                        print('found ACK')
                    contents = decode_payload(77, message['payload'])
                    command_id, result, progress, result_param2, target_system, target_component = contents
                    if [target_component, target_system, command_id] == [self.c_id, self.s_id, cmd_id]:
                        ack = True
                        del read_buffer[idx]
                        if debug:
                            print('ACK accepted')
                        break
                    else:
                        if debug:
                            print('ACK rejected')
                            print(
                                'expected values: cmd_id', cmd_id, 'system_id', self.s_id,
                                'component_id', self.c_id
                            )
                            print(
                                'values received: cmd_id', command_id, 'system_id', target_system,
                                'component_id', target_component
                            )
        return ack

    async def send(
            self,
            command_id: int = 0,
            params: (int, int, int, int, int, int, int) = (0, 0, 0, 0, 0, 0, 0)
    ) -> bool:
        """
        Send a command and wait for the ACK.
        """
        format_override = "BBHBfffffff"  # Command_long
        # TODO: Convert command_id to a negative number and send here.
        return await self.wait(command_id)


class MavLink:
    """
    This is where it all comes together babah.
    """
    def __init__(self, message_ids: list, s_id: int = 0xff, c_id: int = 0xff):
        self.system_id = s_id
        self.component_id = c_id
        for f in formats:
            if f not in message_ids:
                del formats[f]
        self.message_ids = message_ids
        self.heartbeat = Heartbeat()
        self.packet = Packet()
        self.command = Command(s_id, c_id)

    async def heartbeat_wait(self):
        """
        Wait for heartbeat packet.
        """
        return self.heartbeat.wait()

    async def create_message(
            self,
            m_id: int,
            payload: [list, tuple],
            c_flags: int = 0x00,
            i_flags: int = 0x00,
            s_id: int = 0x01,
            c_id: int = 0x01,
    ) -> bytes:
        """
        Formats an outgoing message into a Mavlink packet.
        """
        return await self.packet.create_packet(
            m_id,
            payload,
            c_flags,
            i_flags,
            s_id,
            c_id
        )

    async def send_message(self):
        """
        Transmits our data.
        """
        return await self.packet.send()

    async def send_command(
            self, command_id: int = 0,
            params: (int, int, int, int, int, int, int) = (0, 0, 0, 0, 0, 0, 0)
    ):
        """
        Send a command with a 7 byte payload and wait for ACK.
        """
        return await self.command.send(command_id, params)

    async def receive(self):
        """
        Read from the incoming buffer
        """
        return await self.packet.receive()


async def test(_uart):
    """
    Original reboot method:

        def reboot_autopilot(self, hold_in_bootloader=False):
        '''reboot the autopilot'''
        if self.mavlink10():
            if hold_in_bootloader:
                param1 = 3
            else:
                param1 = 1
            self.mav.command_long_send(self.target_system, self.target_component,
               mavlink.MAV_CMD_PREFLIGHT_REBOOT_SHUTDOWN, 0,
               param1, 0, 0, 0, 0, 0, 0)
    """
    m_id = 246
    m = MavLink([m_id])
    pk = await m.create_message(
        m_id=m_id,
        payload=[1, 1, 1, 0, 0, 0, -1],
        c_flags=0,
        i_flags=0,
        s_id=1,
        c_id=1
    )
    p = list(pk)
    pay_end = 10 + p[1]
    pl = await decode_payload(m_id, p[10:pay_end], debug=True)
    msg = f'start {p[0]}, length {p[1]}, incompat {p[2]}, compat {p[3]}, seq {p[4]}, sys_id {p[5]}, '
    msg += f'comp_id {p[6]}, mes_id {struct.unpack("H", bytes(p[7:9]))[0]}, '
    msg += f'payload {pl}, chk {p[pay_end:pay_end + 2]}'
    read_buffer.append({
        'message_id': struct.unpack("H", bytes(p[7:9]))[0],
        'system_id': p[5],
        'component_id': p[6],
        'payload': p[10:pay_end]
    })
    print(msg)
    await m.send_message()
    await uart_io(_uart, True)
