"""leveldb log datastore.

Format is described at:
    https://github.com/google/leveldb/blob/master/doc/log_format.md

block := record* trailer?
record :=
  checksum: uint32     // crc32c of type and data[] ; little-endian
  length: uint16       // little-endian
  type: uint8          // One of FULL, FIRST, MIDDLE, LAST
  data: uint8[length]

header :=
  ident: char[4]
  magic: uint16
  version: uint8
"""
from __future__ import print_function

import logging
import os
import struct
import sys
import zlib

import wandb

logger = logging.getLogger(__name__)

PY3 = sys.version_info.major == 3 and sys.version_info.minor >= 5

LEVELDBLOG_HEADER_LEN = 7
LEVELDBLOG_BLOCK_LEN = 32768
LEVELDBLOG_DATA_LEN = LEVELDBLOG_BLOCK_LEN - LEVELDBLOG_HEADER_LEN

LEVELDBLOG_FULL = 1
LEVELDBLOG_FIRST = 2
LEVELDBLOG_MIDDLE = 3
LEVELDBLOG_LAST = 4

LEVELDBLOG_HEADER_IDENT = ":W&B"
LEVELDBLOG_HEADER_MAGIC = (
    0xBEE1  # zlib.crc32(bytes("Weights & Biases", 'iso8859-1')) & 0xffff
)
LEVELDBLOG_HEADER_VERSION = 0

try:
    bytes("", "ascii")

    def strtobytes(x):
        """strtobytes."""
        return bytes(x, "iso8859-1")

    # def bytestostr(x):
    #     return str(x, 'iso8859-1')

except Exception:
    strtobytes = str
    # bytestostr = str


class DataStore(object):
    def __init__(self):
        self._opened_for_scan = False
        self._fp = None
        self._index = 0

        self._crc = [0] * (LEVELDBLOG_LAST + 1)
        for x in range(1, LEVELDBLOG_LAST + 1):
            self._crc[x] = zlib.crc32(strtobytes(chr(x))) & 0xFFFFFFFF

        assert wandb._IS_INTERNAL_PROCESS

    def open_for_write(self, fname):
        self._fname = fname
        logger.info("open: %s", fname)
        open_flags = "xb"
        if not PY3:
            open_flags = "wb"
            if os.path.exists(fname):
                raise IOError("File exists: {}".format(fname))
        self._fp = open(fname, open_flags)
        # write header
        self._write_header()

    def open_for_append(self, fname):
        # TODO: implement
        self._fname = fname
        logger.info("open: %s", fname)
        self._fp = open(fname, "wb")
        # do something with _index

    def open_for_scan(self, fname):
        self._fname = fname
        logger.info("open for scan: %s", fname)
        self._fp = open(fname, "rb")
        self._index = 0
        self._opened_for_scan = True

    def scan_record(self):
        assert self._opened_for_scan
        # TODO(jhr): if end of block, deal with up to 6 zero bytes
        header = self._fp.read(LEVELDBLOG_HEADER_LEN)
        if len(header) != LEVELDBLOG_HEADER_LEN:
            return None
        fields = struct.unpack("<IHB", header)
        checksum, dlength, dtype = fields
        data = self._fp.read(dlength)
        # check len
        return data

    def scan_block(self):
        pass

    def scan(self):
        # how much left in the block.  if less than header len, read as pad,
        # verify they are zero
        pass

    def _write_header(self):
        data = struct.pack(
            "<4sHB",
            strtobytes(LEVELDBLOG_HEADER_IDENT),
            LEVELDBLOG_HEADER_MAGIC,
            LEVELDBLOG_HEADER_VERSION,
        )
        assert len(data) == 7
        self._fp.write(data)
        self._index += len(data)

    def _write_record(self, s, dtype=None):
        """Write record that must fit into a block."""
        # double check that there is enough space
        # (this is a precondition to calling this method)
        assert len(s) + LEVELDBLOG_HEADER_LEN <= (
            LEVELDBLOG_BLOCK_LEN - self._index % LEVELDBLOG_BLOCK_LEN
        )

        checksum = 0
        dlength = len(s)
        dtype = dtype or LEVELDBLOG_FULL
        # print("record: length={} type={}".format(dlength, dtype))
        checksum = zlib.crc32(s, self._crc[dtype]) & 0xFFFFFFFF
        self._fp.write(struct.pack("<IHB", checksum, dlength, dtype))
        if dlength:
            self._fp.write(s)
        self._index += LEVELDBLOG_HEADER_LEN + len(s)

    def _write_data(self, s):
        file_offset = self._index
        flush_index = 0
        flush_offset = 0

        offset = self._index % LEVELDBLOG_BLOCK_LEN
        space_left = LEVELDBLOG_BLOCK_LEN - offset
        data_used = 0
        data_left = len(s)
        if space_left < LEVELDBLOG_HEADER_LEN:
            pad = "\x00" * space_left
            self._fp.write(strtobytes(pad))
            self._index += space_left
            offset = 0
            space_left = LEVELDBLOG_BLOCK_LEN

        # does it fit in first (possibly partial) block?
        if data_left + LEVELDBLOG_HEADER_LEN <= space_left:
            self._write_record(s)
        else:
            # write first record (we could still be in the middle of a block,
            # but this write will end on a block boundary)
            data_room = space_left - LEVELDBLOG_HEADER_LEN
            self._write_record(s[:data_room], LEVELDBLOG_FIRST)
            data_used += data_room
            data_left -= data_room
            assert data_left

            # write middles (if any)
            while data_left > LEVELDBLOG_DATA_LEN:
                self._write_record(
                    s[data_used : data_used + LEVELDBLOG_DATA_LEN],  # noqa: E203
                    LEVELDBLOG_MIDDLE,
                )
                data_used += LEVELDBLOG_DATA_LEN
                data_left -= LEVELDBLOG_DATA_LEN

            # write last
            self._write_record(s[data_used:], LEVELDBLOG_LAST)

        return file_offset, self._index - file_offset, flush_index, flush_offset

    def write(self, obj):
        """Write a protocol buffer.

        Args:
            obj: Protocol buffer to write.

        Returns:
            (file_offset, length, flush_index, flush_offset) if successful,
            None otherwise

        """
        raw_size = obj.ByteSize()
        s = obj.SerializeToString()
        assert len(s) == raw_size
        ret = self._write_data(s)
        return ret

    def close(self):
        if self._fp is not None:
            logger.info("close: %s", self._fname)
            self._fp.close()
