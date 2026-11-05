#!/usr/bin/env python
"""find_prompt() and the prompts still owed for the extra RETURNs it sent.

A device slower to answer RETURN than find_prompt() polls gets more than one RETURN
and answers each with its own prompt. If find_prompt() returns on the first one, the
rest land in front of the next command's echo, and send_command() with cmd_verify off
matches them and returns before the output arrives.

The fake device below handles its input strictly in order, the way a CLI does. All
timings are scaled down by global_delay_factor=0.1, so find_prompt() polls every
25 ms and a "slow" device here answers RETURN in 60 ms.
"""

import time
from threading import Lock

import pytest

from netmiko.base_connection import BaseConnection
from netmiko.tplink.tplink_jetstream import TPLinkJetStreamBase

PROMPT = "switch1#"
BODY = "!model\n#\nvlan 1\n name System\n#\n" + "#\n".join(
    f"interface 1/0/{i}\n" for i in range(1, 30)
)
BODY += "end\n"


class SerialDevice:
    """Answers its input in order: each RETURN costs `latency`, then a prompt."""

    def __init__(self, latency, drop_typeahead=False):
        self.latency = latency
        self.drop_typeahead = drop_typeahead
        self.pending = []  # (ready_at, text)
        self.busy_until = 0.0
        self.returns = 0

    def write(self, data):
        now = time.time()
        if self.drop_typeahead and now < self.busy_until:
            return
        start = max(now, self.busy_until)
        if data == "\n":
            self.returns += 1
            self.busy_until = start + self.latency
            self.pending.append((self.busy_until, "\n" + PROMPT))
            return
        ready = start + 0.002
        self.pending.append((ready, data.strip() + "\n"))
        for line in BODY.splitlines(keepends=True):
            ready += 0.001
            self.pending.append((ready, line))
        ready += 0.002
        self.pending.append((ready, PROMPT))
        self.busy_until = ready

    def read(self):
        now = time.time()
        out = ""
        self.pending.sort()
        while self.pending and self.pending[0][0] <= now:
            out += self.pending.pop(0)[1]
        return out


def connect(device, cls=BaseConnection):
    class FakeConnection(cls):
        def __init__(self):
            self._session_locker = Lock()
            self.RETURN = "\n"
            self.RESPONSE_RETURN = "\n"
            self.global_cmd_verify = False
            self.base_prompt = PROMPT[:-1]
            self.read_timeout_override = None
            self.delay_factor_compat = False
            self.global_delay_factor = 0.1
            self.fast_cli = True
            self.device_type = "fake"
            self.ansi_escape_codes = False
            self.disable_lf_normalization = False
            self.session_log = None
            self._read_buffer = ""
            self.protocol = "ssh"

        def write_channel(self, out_data):
            device.write(out_data)

        def read_channel(self):
            out = self._read_buffer + device.read()
            self._read_buffer = ""
            return out

    return FakeConnection()


@pytest.mark.parametrize("cls", [BaseConnection, TPLinkJetStreamBase])
@pytest.mark.parametrize("latency", [0.005, 0.06, 0.15])
def test_send_command_returns_the_whole_output(cls, latency):
    """A slow prompt must not end the next command's read before its output."""
    conn = connect(SerialDevice(latency), cls)
    output = conn.send_command("show running-config", read_timeout=5)
    assert output.startswith("!model")
    assert output.rstrip().endswith("end")


def test_find_prompt_leaves_no_prompt_pending():
    device = SerialDevice(0.06)
    conn = connect(device)
    assert conn.find_prompt() == PROMPT
    assert device.returns > 1  # the slow path this is about
    time.sleep(0.3)
    assert device.read() == ""


def test_fast_device_gets_a_single_return():
    """The common case is untouched: one RETURN, no extra wait."""
    device = SerialDevice(0.005)
    conn = connect(device)
    started = time.time()
    assert conn.find_prompt() == PROMPT
    assert device.returns == 1
    assert time.time() - started < 0.2


def test_device_dropping_typeahead_is_bounded():
    """A device that never answers the extra RETURNs costs a bounded wait."""
    device = SerialDevice(0.06, drop_typeahead=True)
    conn = connect(device)
    started = time.time()
    assert conn.find_prompt() == PROMPT
    assert time.time() - started < 1.0
    output = conn.send_command("show running-config", read_timeout=5)
    assert output.rstrip().endswith("end")
