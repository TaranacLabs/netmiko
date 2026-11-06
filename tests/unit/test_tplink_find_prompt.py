#!/usr/bin/env python
"""TP-Link JetStream: find_prompt() must not leave prompts pending for send_command().

The fake device below behaves like a JetStream CLI: it handles its input strictly in
order and answers RETURN with a bare newline, then the prompt in a later segment.
"""

import time
from threading import Lock

import pytest

from netmiko.base_connection import BaseConnection
from netmiko.tplink.tplink_jetstream import TPLinkJetStreamBase

PROMPT = "switch1#"
BODY = "!SG2218P\n#\nhostname switch1\n#\n" + "#\n".join(
    f"interface gigabitEthernet 1/0/{i}\n" for i in range(1, 30)
)
BODY += "end\n"


class JetStreamCLI:
    """Answers RETURN with "\\n" now and the prompt `latency` later, in input order.

    With `split`, the prompt's terminator arrives `split` seconds after the hostname.
    """

    def __init__(self, latency, split=0.0):
        self.latency = latency
        self.split = split
        self.pending = []  # (ready_at, text)
        self.busy_until = 0.0
        self.returns = 0

    def write(self, data):
        start = max(time.time(), self.busy_until)
        if data.strip() == "":
            self.returns += 1
            self.pending.append((start + 0.002, "\n"))
            self.busy_until = start + self.latency
            if self.split:
                self.pending.append((self.busy_until, "\n" + PROMPT[:-1]))
                self.busy_until += self.split
                self.pending.append((self.busy_until, PROMPT[-1]))
            else:
                self.pending.append((self.busy_until, "\n" + PROMPT))
            return
        ready = start + 0.002
        self.pending.append((ready, data.strip() + "\n"))
        for line in BODY.splitlines(keepends=True):
            ready += 0.001
            self.pending.append((ready, line))
        ready += 0.002
        self.pending.append((ready, "\n" + PROMPT))
        self.busy_until = ready

    def read(self):
        now = time.time()
        out = ""
        self.pending.sort(key=lambda item: item[0])
        while self.pending and self.pending[0][0] <= now:
            out += self.pending.pop(0)[1]
        return out


def connect(device, cls):
    class FakeConnection(cls):
        def __init__(self):
            self._session_locker = Lock()
            self.RETURN = "\r\n"
            self.RESPONSE_RETURN = "\n"
            self.global_cmd_verify = False
            self.base_prompt = PROMPT[:-1]
            self.read_timeout_override = None
            self.delay_factor_compat = False
            self.global_delay_factor = 0.1
            self.fast_cli = True
            self.device_type = "tplink_jetstream"
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


@pytest.mark.parametrize("latency", [0.005, 0.05, 0.2])
def test_send_command_returns_the_whole_output(latency):
    conn = connect(JetStreamCLI(latency), TPLinkJetStreamBase)
    output = conn.send_command("show running-config", read_timeout=5)
    assert output.startswith("!SG2218P")
    assert output.rstrip().endswith("end")


@pytest.mark.parametrize("latency", [0.005, 0.05, 0.2])
def test_find_prompt_sends_a_single_return(latency):
    device = JetStreamCLI(latency)
    conn = connect(device, TPLinkJetStreamBase)
    assert conn.find_prompt() == PROMPT
    assert device.returns == 1


def test_generic_find_prompt_leaves_a_prompt_pending():
    """The failure the override avoids, shown with the generic implementation."""
    device = JetStreamCLI(0.05)
    conn = connect(device, BaseConnection)
    assert conn.find_prompt() == PROMPT
    assert device.returns > 1
    time.sleep(0.3)
    assert PROMPT in device.read()


def test_send_command_waits_for_a_split_prompt():
    """The hostname arrives without its terminator, which is also a line of the config."""
    conn = connect(JetStreamCLI(0.005, split=0.1), TPLinkJetStreamBase)
    assert conn.find_prompt() == PROMPT
    output = conn.send_command("show running-config", read_timeout=5)
    assert output.rstrip().endswith("end")


def test_generic_find_prompt_returns_a_split_prompt_without_terminator():
    """The failure the override avoids: send_command() stops at "hostname switch1"."""
    conn = connect(JetStreamCLI(0.005, split=0.1), BaseConnection)
    assert conn.find_prompt() == PROMPT[:-1]
    output = conn.send_command("show running-config", read_timeout=5)
    assert not output.rstrip().endswith("end")
